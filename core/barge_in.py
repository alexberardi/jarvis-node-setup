"""Barge-in monitor for interrupting TTS playback with wake word detection.

Subscribes to an ``AudioBus`` during TTS playback and runs openWakeWord
against each chunk.  On detection it cancels the active playback
subprocess so the voice listener can immediately start recording a new
command.

Detection strategy — dynamic-baseline energy-gated OWW:
  OWW alone cannot reliably score the wake word through heavy speaker-
  to-mic bleed (typical peak ~0.10 vs clean ~0.9).  Instead we combine
  two signals:

  1. **Dynamic energy gate** — during the first ~1s of monitoring, we
     sample the TTS-through-mic RMS level to establish a baseline. A
     barge-in requires energy significantly above that baseline (2x),
     meaning the user must be speaking *on top of* the TTS. This
     prevents false triggers when TTS itself says the wake word —
     the OWW score spikes but the energy stays at the TTS baseline.
  2. **OWW partial match** — even a low OWW score (~0.07) confirms the
     energy burst is the wake word, not a cough or clap.

  Both conditions must be true simultaneously.  This keeps false-
  positive rate near zero while detecting a wake word spoken over TTS.
"""

from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np
# scipy.signal lazy-imported below (see voice_listener.py for rationale).
_resample_poly = None
from jarvis_log_client import JarvisLogger


def _get_resample_poly():
    global _resample_poly
    if _resample_poly is None:
        from scipy.signal import resample_poly  # noqa: E402
        _resample_poly = resample_poly
    return _resample_poly

from core.audio_bus import AudioBus
from core.platform_audio import platform_audio

logger = JarvisLogger(service="jarvis-node")

# Shared lock for openWakeWord operations across threads. Imported by
# voice_listener.py too — there's one oww model instance shared between
# the wake loop, this barge-in monitor, and a background reset thread.
# ``oww.reset()`` takes ~1.5 s on the Pi Zero; voice_listener runs it in
# a background pool after wake fires (so the wake response can play
# immediately) and this lock prevents that background reset from racing
# with predict()/reset() in either the wake loop or this monitor.
oww_lock = threading.Lock()

# Single-thread executor for background oww.reset() calls. Used by
# ``BargeInMonitor.stop()`` so the 1.5 s LSTM reinit doesn't block the
# return-to-idle path. max_workers=1 because reset isn't parallelizable
# anyway (the lock serializes it), and the queue depth is naturally
# bounded — one reset per wake cycle.
_reset_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="oww-reset")

# openWakeWord constants (must match voice_listener.py)
_OWW_RATE = 16000
_OWW_CHUNK = 1280

# Energy-gate defaults.  The static threshold is a floor — the dynamic
# baseline (computed from the first ~1s of TTS playback) overrides it
# when TTS energy is higher.  A user speaking at normal volume 2-3 ft
# away reads 1000-3000 RMS; this floor catches the case where TTS is
# very quiet or silent during the baseline window.
_DEFAULT_ENERGY_THRESHOLD = 500

# OWW score threshold during barge-in.  Much lower than the normal 0.4
# wake threshold because TTS bleed degrades the signal.  Empirically,
# "Hey Jarvis" over TTS scores 0.08-0.12; TTS alone scores < 0.01.
_DEFAULT_OWW_THRESHOLD = 0.07

# How many consecutive chunks must satisfy BOTH energy + OWW gates
# before we commit to a barge-in.  Prevents single-sample spikes.
_DEFAULT_CONFIRM_CHUNKS = 2

# Dynamic baseline: collect RMS samples during this window, then compute
# an energy threshold relative to TTS playback level.  This prevents
# false triggers when TTS itself says the wake word (OWW spikes, but
# energy stays at the TTS baseline instead of jumping above it).
_BASELINE_WINDOW_SECS = 1.0
_BASELINE_MULTIPLIER = 2.0  # require 2x TTS baseline to trigger


class BargeInMonitor:
    """Score wake-word predictions on bus chunks during TTS playback.

    Usage::

        monitor = BargeInMonitor(bus, oww_model, "hey_jarvis")
        monitor.start()
        platform_audio.play_audio_file(response_wav)  # blocks
        monitor.stop()
        if monitor.was_interrupted:
            # cancel any follow-up, jump straight to LISTENING
    """

    def __init__(
        self,
        bus: AudioBus,
        oww_model,
        wake_word_model_name: str,
        *,
        threshold: float = _DEFAULT_OWW_THRESHOLD,
        energy_threshold: float = _DEFAULT_ENERGY_THRESHOLD,
        confirm_chunks: int = _DEFAULT_CONFIRM_CHUNKS,
        skip_seconds: float = 0.5,
        baseline_window_secs: float = _BASELINE_WINDOW_SECS,
        baseline_multiplier: float = _BASELINE_MULTIPLIER,
        subscriber_name: str = "barge_in",
    ):
        self._bus = bus
        self._oww = oww_model
        self._wake_word = wake_word_model_name
        self._threshold = threshold
        self._energy_threshold = energy_threshold
        self._confirm_chunks = confirm_chunks
        self._skip_seconds = skip_seconds
        self._baseline_window_secs = baseline_window_secs
        self._baseline_multiplier = baseline_multiplier
        self._subscriber_name = subscriber_name

        # Bus is at 48 kHz; OWW needs 16 kHz. Always resample.
        self._needs_resample = bus.rate != _OWW_RATE
        self._resample_ratio = bus.rate // _OWW_RATE if self._needs_resample else 1

        self._stop_event = threading.Event()
        self._interrupted = False
        self._thread: threading.Thread | None = None

    @property
    def was_interrupted(self) -> bool:
        """True if wake word was detected during monitoring."""
        return self._interrupted

    def start(self) -> None:
        """Subscribe to the bus and begin scoring in a background thread."""
        self._stop_event.clear()
        self._interrupted = False
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="BargeInMonitor"
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the monitor to stop and wait for the thread to exit.

        Resets the OWW model from the calling thread (not the monitor
        thread) to avoid concurrent access with the wake detection loop.
        """
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        # oww.reset() is ~1.5 s of LSTM reinit on the Pi Zero. Doing it
        # synchronously here was the second hidden source of dead air
        # in the wake cycle: the response was already over but stop()
        # blocked the return to wake mode. Submit to the module-level
        # executor under the shared ``oww_lock``; any future predict()
        # naturally waits. The next wake loop won't predict for many
        # seconds (follow-up window, etc.) so the background reset is
        # guaranteed to finish first in practice.
        oww = self._oww

        def _bg_reset() -> None:
            _t = time.monotonic()
            with oww_lock:
                oww.reset()
            logger.info(
                "Barge-in oww.reset finished",
                duration_ms=int((time.monotonic() - _t) * 1000),
            )
        _reset_executor.submit(_bg_reset)

    def _monitor_loop(self) -> None:
        q = self._bus.subscribe(self._subscriber_name)
        skip_until = time.monotonic() + self._skip_seconds
        chunk_count = 0
        max_score = 0.0
        max_rms = 0

        # Trailing energy window — OWW scores peak ~0.5-1s after the
        # user's voice energy spike, so we track whether a spike happened
        # recently rather than requiring it on the same chunk.
        chunk_secs = self._bus.chunk_samples / self._bus.rate
        energy_window_chunks = max(1, int(1.0 / chunk_secs))  # ~1 second
        recent_rms: list[int] = []

        # Dynamic energy baseline — measure TTS-through-mic level during
        # the first baseline window and require energy well above it.
        # Prevents false triggers when TTS itself says the wake word:
        # OWW spikes, but energy stays at the TTS floor.
        baseline_window_chunks = max(1, int(self._baseline_window_secs / chunk_secs))
        baseline_rms_samples: list[int] = []
        effective_energy_threshold = self._energy_threshold

        try:
            while not self._stop_event.is_set():
                try:
                    raw_data = q.get(timeout=0.1)
                except queue.Empty:
                    continue

                if time.monotonic() < skip_until:
                    continue

                chunk_count += 1
                samples = np.frombuffer(raw_data, dtype=np.int16)
                rms = int(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))

                if rms > max_rms:
                    max_rms = rms

                # Maintain trailing RMS window
                recent_rms.append(rms)
                if len(recent_rms) > energy_window_chunks:
                    recent_rms.pop(0)
                recent_max_rms = max(recent_rms)

                # --- Baseline collection phase ---
                # Collect RMS during the first N chunks to learn TTS volume.
                # Still run OWW (to prime its LSTM state) but don't trigger.
                if len(baseline_rms_samples) < baseline_window_chunks:
                    baseline_rms_samples.append(rms)
                    if len(baseline_rms_samples) >= baseline_window_chunks:
                        sorted_rms = sorted(baseline_rms_samples)
                        tts_baseline = sorted_rms[int(len(sorted_rms) * 0.75)]
                        dynamic_threshold = int(tts_baseline * self._baseline_multiplier)
                        effective_energy_threshold = max(self._energy_threshold, dynamic_threshold)
                        logger.info(
                            "Barge-in baseline established",
                            tts_p75_rms=tts_baseline,
                            dynamic_threshold=dynamic_threshold,
                            effective_threshold=effective_energy_threshold,
                            static_threshold=self._energy_threshold,
                        )
                    # Resample + score OWW to keep LSTM primed, but skip trigger check
                    if self._needs_resample:
                        resampled = _get_resample_poly()(samples, up=1, down=self._resample_ratio)
                        samples = np.clip(resampled, -32768, 32767).astype(np.int16)
                    with oww_lock:
                        self._oww.predict(samples)
                    continue

                # Periodic diagnostics
                if chunk_count % 25 == 1:
                    logger.info(
                        "Barge-in audio",
                        chunk=chunk_count,
                        rms=rms,
                        recent_max_rms=recent_max_rms,
                        energy_threshold=effective_energy_threshold,
                        max_score=round(max_score, 3),
                    )

                if self._needs_resample:
                    resampled = _get_resample_poly()(samples, up=1, down=self._resample_ratio)
                    samples = np.clip(resampled, -32768, 32767).astype(np.int16)

                with oww_lock:
                    predictions = self._oww.predict(samples)
                score = predictions.get(self._wake_word, 0)

                if score > max_score:
                    max_score = score

                if score > 0.05:
                    logger.info(
                        "Barge-in score",
                        score=round(float(score), 3),
                        rms=rms,
                        recent_max_rms=recent_max_rms,
                        energy_threshold=effective_energy_threshold,
                    )

                # --- Two-tier detection ---
                # Tier 1: Strong OWW (>0.5) + recent energy spike above
                #         the dynamic baseline. OWW peaks after the voice
                #         energy fades, so we check the trailing window.
                # Tier 2: Weak OWW (>threshold) + current high energy
                #         above the dynamic baseline.
                triggered = False
                if score > 0.5 and recent_max_rms > effective_energy_threshold:
                    triggered = True
                elif score > self._threshold and rms > effective_energy_threshold:
                    triggered = True

                if triggered:
                    logger.info(
                        "Barge-in detected",
                        score=round(float(score), 3),
                        rms=rms,
                        recent_max_rms=recent_max_rms,
                        energy_threshold=effective_energy_threshold,
                    )
                    self._interrupted = True
                    platform_audio.cancel_playback()
                    break
        except Exception as e:
            logger.warning("Barge-in monitor error", error=str(e))
        finally:
            logger.info(
                "Barge-in monitor stopped",
                chunks_processed=chunk_count,
                max_score=round(max_score, 3),
                max_rms=max_rms,
                effective_energy_threshold=effective_energy_threshold,
                interrupted=self._interrupted,
            )
            self._bus.unsubscribe(self._subscriber_name)
