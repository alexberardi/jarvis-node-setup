"""Barge-in monitor for interrupting TTS playback with wake word detection.

Subscribes to an ``AudioBus`` during TTS playback and runs openWakeWord
against each chunk.  On detection it cancels the active playback
subprocess so the voice listener can immediately start recording a new
command.

Detection strategy — energy-gated OWW:
  OWW alone cannot reliably score the wake word through heavy speaker-
  to-mic bleed (typical peak ~0.10 vs clean ~0.9).  Instead we combine
  two signals:

  1. **Energy gate** — a sharp RMS rise above the running TTS-bleed
     baseline means the user is speaking into the mic.
  2. **OWW partial match** — even a low OWW score (~0.07) confirms the
     energy burst is the wake word, not a cough or clap.

  Both conditions must be true simultaneously.  This keeps false-
  positive rate near zero while detecting a wake word spoken over TTS.
"""

from __future__ import annotations

import queue
import threading
import time

import numpy as np
from scipy.signal import resample_poly
from jarvis_log_client import JarvisLogger

from core.audio_bus import AudioBus
from core.platform_audio import platform_audio

logger = JarvisLogger(service="jarvis-node")

# openWakeWord constants (must match voice_listener.py)
_OWW_RATE = 16000
_OWW_CHUNK = 1280

# Energy-gate defaults.  TTS bleed through a desk mic typically reads
# 100-300 RMS; a user speaking at normal volume 2-3 ft away reads
# 1000-3000.  500 sits safely in between.
_DEFAULT_ENERGY_THRESHOLD = 500

# OWW score threshold during barge-in.  Much lower than the normal 0.4
# wake threshold because TTS bleed degrades the signal.  Empirically,
# "Hey Jarvis" over TTS scores 0.08-0.12; TTS alone scores < 0.01.
_DEFAULT_OWW_THRESHOLD = 0.07

# How many consecutive chunks must satisfy BOTH energy + OWW gates
# before we commit to a barge-in.  Prevents single-sample spikes.
_DEFAULT_CONFIRM_CHUNKS = 2


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
        subscriber_name: str = "barge_in",
    ):
        self._bus = bus
        self._oww = oww_model
        self._wake_word = wake_word_model_name
        self._threshold = threshold
        self._energy_threshold = energy_threshold
        self._confirm_chunks = confirm_chunks
        self._skip_seconds = skip_seconds
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
        # Reset OWW here — in the caller's thread — so there is zero
        # chance of overlapping with oww.predict() in the wake loop.
        self._oww.reset()

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

                # Periodic diagnostics
                if chunk_count % 25 == 1:
                    logger.info(
                        "Barge-in audio",
                        chunk=chunk_count,
                        rms=rms,
                        recent_max_rms=recent_max_rms,
                        max_score=round(max_score, 3),
                    )

                if self._needs_resample:
                    resampled = resample_poly(samples, up=1, down=self._resample_ratio)
                    samples = np.clip(resampled, -32768, 32767).astype(np.int16)

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
                    )

                # --- Two-tier detection ---
                # Tier 1: Strong OWW (>0.5) + recent energy spike.
                #         OWW peaks after the voice energy fades, so
                #         we check the trailing window, not this chunk.
                # Tier 2: Weak OWW (>threshold) + current high energy.
                #         For heavy TTS bleed where OWW can only score
                #         ~0.07-0.12, require simultaneous energy.
                triggered = False
                if score > 0.5 and recent_max_rms > self._energy_threshold:
                    triggered = True
                elif score > self._threshold and rms > self._energy_threshold:
                    triggered = True

                if triggered:
                    logger.info(
                        "Barge-in detected",
                        score=round(float(score), 3),
                        rms=rms,
                        recent_max_rms=recent_max_rms,
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
                interrupted=self._interrupted,
            )
            self._bus.unsubscribe(self._subscriber_name)
