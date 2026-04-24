"""Barge-in monitor for interrupting TTS playback with wake word detection.

Subscribes to an ``AudioBus`` during TTS playback and runs openWakeWord
against each chunk. On detection it cancels the active playback
subprocess so the voice listener can immediately start recording a new
command.

Key change from the pre-AudioBus version: this no longer opens its own
PyAudio stream. Concurrent PyAudio opens on dsnoop are exactly the
race surface we're eliminating — the bus owns the mic, we just pull
from its queue.

Uses an elevated confidence threshold (default 0.7 vs normal 0.4) to
reduce false triggers from speaker-to-mic audio bleed during TTS.
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
        threshold: float = 0.7,
        skip_seconds: float = 0.5,
        subscriber_name: str = "barge_in",
    ):
        self._bus = bus
        self._oww = oww_model
        self._wake_word = wake_word_model_name
        self._threshold = threshold
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
        """Signal the monitor to stop and wait for the thread to exit."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _monitor_loop(self) -> None:
        q = self._bus.subscribe(self._subscriber_name)
        chunk_secs = self._bus.chunk_samples / self._bus.rate
        skip_until = time.monotonic() + self._skip_seconds

        try:
            while not self._stop_event.is_set():
                try:
                    raw_data = q.get(timeout=0.1)
                except queue.Empty:
                    continue

                if time.monotonic() < skip_until:
                    # Discard initial chunks to dodge TTS onset bleed
                    continue

                samples = np.frombuffer(raw_data, dtype=np.int16)
                if self._needs_resample:
                    resampled = resample_poly(samples, up=1, down=self._resample_ratio)
                    samples = np.clip(resampled, -32768, 32767).astype(np.int16)

                predictions = self._oww.predict(samples)
                score = predictions.get(self._wake_word, 0)

                if score > self._threshold:
                    logger.info(
                        "Barge-in detected",
                        score=round(float(score), 3),
                        threshold=self._threshold,
                    )
                    self._interrupted = True
                    platform_audio.cancel_playback()
                    self._oww.reset()
                    break

                _ = chunk_secs  # timing for future metric use
        except Exception as e:
            logger.debug("Barge-in monitor error (non-fatal)", error=str(e))
        finally:
            self._bus.unsubscribe(self._subscriber_name)
