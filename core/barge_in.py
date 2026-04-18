"""Barge-in monitor for interrupting TTS playback with wake word detection.

Runs openWakeWord inference in a background thread during audio playback.
When the wake word is detected, cancels the active playback subprocess so
the voice listener can immediately start recording a new command.

Uses an elevated confidence threshold (default 0.7 vs normal 0.4) to
reduce false triggers from speaker-to-mic audio bleed during TTS.
"""

import threading
import time

import numpy as np
import pyaudio
from scipy.signal import resample_poly
from jarvis_log_client import JarvisLogger

from core.platform_audio import platform_audio

logger = JarvisLogger(service="jarvis-node")

# openWakeWord constants (must match voice_listener.py)
_OWW_RATE = 16000
_OWW_CHUNK = 1280
_MIC_RATE = 48000
_MIC_CHUNK = _OWW_CHUNK * (_MIC_RATE // _OWW_RATE)


class BargeInMonitor:
    """Monitor for wake word during TTS playback.

    Usage::

        monitor = BargeInMonitor(oww_model, "hey_jarvis", ...)
        monitor.start()
        # ... TTS plays in another thread or blocking call ...
        monitor.stop()
        if monitor.was_interrupted:
            # wake word detected — cancel TTS, listen for new command
    """

    def __init__(
        self,
        oww_model,
        wake_word_model_name: str,
        threshold: float = 0.7,
        needs_resample: bool = True,
        mic_device_index: int | None = None,
        skip_seconds: float = 0.5,
    ):
        self._oww = oww_model
        self._wake_word = wake_word_model_name
        self._threshold = threshold
        self._needs_resample = needs_resample
        self._mic_index = mic_device_index
        self._skip_seconds = skip_seconds

        self._stop_event = threading.Event()
        self._interrupted = False
        self._thread: threading.Thread | None = None

    @property
    def was_interrupted(self) -> bool:
        """True if wake word was detected during monitoring."""
        return self._interrupted

    def start(self) -> None:
        """Start monitoring in a background daemon thread."""
        self._stop_event.clear()
        self._interrupted = False
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Signal the monitor to stop and wait for it."""
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _monitor_loop(self) -> None:
        """Background thread: open mic, run OWW, cancel playback on detection."""
        pa: pyaudio.PyAudio | None = None
        stream: pyaudio.Stream | None = None

        chunk_size = _MIC_CHUNK if self._needs_resample else _OWW_CHUNK
        rate = _MIC_RATE if self._needs_resample else _OWW_RATE

        try:
            pa = pyaudio.PyAudio()
            open_kwargs: dict = dict(
                channels=1,
                format=pyaudio.paInt16,
                input=True,
                rate=rate,
                frames_per_buffer=chunk_size,
            )
            if self._mic_index is not None:
                open_kwargs["input_device_index"] = self._mic_index
            stream = pa.open(**open_kwargs)

            # Skip initial frames to avoid TTS onset bleed
            skip_frames = int(self._skip_seconds * rate / chunk_size)
            frame_idx = 0

            while not self._stop_event.is_set():
                try:
                    raw_data = stream.read(chunk_size, exception_on_overflow=False)
                except OSError:
                    break

                frame_idx += 1
                if frame_idx <= skip_frames:
                    continue

                samples = np.frombuffer(raw_data, dtype=np.int16)
                if self._needs_resample:
                    resampled = resample_poly(samples, up=1, down=3)
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

        except Exception as e:
            logger.debug("Barge-in monitor error (non-fatal)", error=str(e))
        finally:
            if stream is not None:
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
            if pa is not None:
                try:
                    pa.terminate()
                except Exception:
                    pass
