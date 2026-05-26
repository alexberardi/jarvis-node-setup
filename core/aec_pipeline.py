"""Inline acoustic-echo-cancellation pipeline.

Combines the Speex echo canceller (``core.aec_speex.EchoCanceller``)
with the parec-driven reference reader (``core.aec_reference.ReferenceReader``)
and exposes a single ``.process(mic) -> cleaned`` call to be inserted
into the wake-word path in ``voice_listener.py``.

When no playback is active, the reference reader returns ``None`` and
this pipeline passes the mic chunk through unchanged — AEC adds zero
cost (and no risk) in quiet rooms.

Public surface::

    pipeline = AecPipeline(
        rate=16000,
        frame_size=160,
        filter_length=1600,
        reference_delay_samples=1280,  # 80 ms speaker->mic
    )
    pipeline.start()
    try:
        cleaned = pipeline.process(mic_16k_int16)
        score = oww.predict(cleaned)
    finally:
        pipeline.stop()
"""

from __future__ import annotations

from collections import deque

import numpy as np

from core.aec_reference import ReferenceReader
from core.aec_speex import EchoCanceller
from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


class AecPipeline:
    """Thin coordinator: pulls aligned reference, runs Speex, falls back gracefully.

    The mic chunk passed to ``process`` must be 16-bit signed int mono
    at ``rate`` Hz, with length a multiple of ``frame_size`` (Speex's
    per-call window). For the wake path that's 1280 samples per 80 ms
    chunk after the 48→16 kHz downsample, which is 8 × 160-sample
    Speex frames.

    ``reference_delay_samples`` shifts the reference read window into
    the past to compensate for the speaker→air→mic acoustic delay
    (typically 30-80 ms on the Pi Zero 2 W + ReSpeaker HAT). A fixed
    value is the v0 approach; step 13 will calibrate it per-node via
    a startup chirp + cross-correlation.
    """

    def __init__(
        self,
        *,
        rate: int = 16000,
        frame_size: int = 160,
        filter_length: int = 1600,
        reference_delay_samples: int = 1280,
        reference_buffer_secs: float = 2.0,
        reference_stale_threshold_ms: float = 200.0,
        monitor_source: str | None = None,
    ):
        if reference_delay_samples < 0:
            raise ValueError(
                f"reference_delay_samples must be non-negative, got {reference_delay_samples}"
            )

        self.rate = rate
        self.frame_size = frame_size
        self.reference_delay_samples = reference_delay_samples

        self._echo = EchoCanceller(
            frame_size=frame_size,
            filter_length=filter_length,
            sample_rate=rate,
        )
        self._reference = ReferenceReader(
            monitor_source=monitor_source,
            rate=rate,
            buffer_secs=reference_buffer_secs,
            stale_threshold_ms=reference_stale_threshold_ms,
        )

        self._process_count = 0
        self._bypass_count = 0
        self._aec_count = 0
        self._error_count = 0

        # Rolling window of per-frame suppression_db values, only populated
        # when ref_rms > 0 (i.e. AEC actually had a non-silent reference to
        # cancel against). ~5 s at 80 ms chunks = 62 entries.
        self._suppression_window: deque[float] = deque(maxlen=62)

    def start(self) -> None:
        self._reference.start()
        logger.info(
            "AEC pipeline started",
            rate=self.rate,
            frame_size=self.frame_size,
            filter_length=self._echo.filter_length,
            reference_delay_samples=self.reference_delay_samples,
        )

    def stop(self) -> None:
        try:
            self._reference.stop()
        finally:
            self._echo.close()
        logger.info(
            "AEC pipeline stopped",
            process_count=self._process_count,
            aec_count=self._aec_count,
            bypass_count=self._bypass_count,
            error_count=self._error_count,
        )

    def process(self, mic: np.ndarray) -> np.ndarray:
        """Run AEC if reference is fresh; pass through unchanged otherwise."""
        self._process_count += 1
        ref = self._reference.pull(
            n_samples=mic.shape[0],
            delay_samples=self.reference_delay_samples,
        )

        mic_rms = float(np.sqrt(np.mean(mic.astype(np.float64) ** 2)))
        if ref is None:
            self._bypass_count += 1
            self._maybe_log_stats(mic_rms=mic_rms, ref_rms=None, cleaned_rms=mic_rms)
            return mic
        try:
            cleaned = self._echo.process(mic, ref)
            self._aec_count += 1
            ref_rms = float(np.sqrt(np.mean(ref.astype(np.float64) ** 2)))
            cleaned_rms = float(np.sqrt(np.mean(cleaned.astype(np.float64) ** 2)))
            # Only record suppression when reference actually contained signal —
            # silence-vs-silence gives nonsense ratios and would poison the window.
            if ref_rms > 100.0 and mic_rms > 0 and cleaned_rms > 0:
                self._suppression_window.append(
                    20.0 * float(np.log10(mic_rms / max(cleaned_rms, 1.0)))
                )
            self._maybe_log_stats(mic_rms=mic_rms, ref_rms=ref_rms, cleaned_rms=cleaned_rms)
            return cleaned
        except Exception as exc:
            self._error_count += 1
            logger.warning(
                "AEC process error; passing mic through unchanged",
                error=str(exc),
            )
            return mic

    def _maybe_log_stats(self, mic_rms: float, ref_rms: float | None, cleaned_rms: float) -> None:
        """Periodic visibility into AEC behavior — every ~5 seconds at 80ms chunks."""
        if self._process_count % 60 != 0:
            return
        suppression_db: float | None = None
        if ref_rms is not None and cleaned_rms > 0 and mic_rms > 0:
            suppression_db = round(20 * float(np.log10(mic_rms / max(cleaned_rms, 1.0))), 1)
        logger.info(
            "AEC stats",
            process_count=self._process_count,
            aec_count=self._aec_count,
            bypass_count=self._bypass_count,
            error_count=self._error_count,
            mic_rms=round(mic_rms, 1),
            ref_rms=None if ref_rms is None else round(ref_rms, 1),
            cleaned_rms=round(cleaned_rms, 1),
            suppression_db=suppression_db,
        )

    @property
    def stats(self) -> dict[str, int]:
        return {
            "process_count": self._process_count,
            "aec_count": self._aec_count,
            "bypass_count": self._bypass_count,
            "error_count": self._error_count,
        }

    def recent_suppression_db(self, min_samples: int = 12) -> float | None:
        """Median suppression (dB) over the recent window, or None if too few samples.

        Only frames where the reference actually had signal contribute, so a
        return value of ``None`` means either no playback was active recently
        or AEC hasn't been running long enough to judge.
        """
        if len(self._suppression_window) < min_samples:
            return None
        sorted_vals = sorted(self._suppression_window)
        return sorted_vals[len(sorted_vals) // 2]

    def is_strongly_active(self, threshold_db: float = 5.0, min_samples: int = 12) -> bool:
        """True when AEC is consistently cancelling enough to be trusted.

        Callers can use this to relax downstream heuristics like the music
        energy gate — if AEC is doing real work, the cleaned OWW score is
        the right signal to trust, not raw mic RMS.
        """
        median = self.recent_suppression_db(min_samples=min_samples)
        return median is not None and median >= threshold_db

    def calibrate_delay(self, bus) -> bool:
        """Measure the speaker→mic delay via a startup chirp.

        Plays a short chirp, captures the round-trip through the existing
        bus + reference reader, and updates ``self.reference_delay_samples``
        with the measured value if cross-correlation finds a clean peak.
        Returns True on success. Failure leaves the existing delay unchanged.
        """
        from core.aec_calibrate import calibrate_speaker_mic_delay

        measured = calibrate_speaker_mic_delay(
            bus=bus,
            reference_reader=self._reference,
            aec_rate=self.rate,
        )
        if measured is None:
            return False
        previous = self.reference_delay_samples
        self.reference_delay_samples = measured
        logger.info(
            "AEC delay updated by calibration",
            previous_samples=previous,
            previous_ms=round(previous * 1000.0 / self.rate, 1),
            measured_samples=measured,
            measured_ms=round(measured * 1000.0 / self.rate, 1),
        )
        return True
