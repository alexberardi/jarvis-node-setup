"""Command-capture primitives over an AudioBus.

These are pure functions: they subscribe to the bus, consume chunks
until a stopping condition (silence, speech-onset, timeout), write a
WAV to the cache dir, and return a RecordingResult. They do not open
PyAudio. They do not resolve devices. The bus owns the mic; we just
read from it.

The old `skip_frames` hack (discarding the first 0.3s/1.0s of a fresh
PyAudio open to dodge TTS bleed) is gone. Its job is now done by
subscribing with ``history_secs`` at the state-machine layer — the
first second of "recording" is replayed from the ring buffer, so
audio the user emitted during the tail of TTS playback is captured
rather than discarded.
"""

from __future__ import annotations

import queue
import time
import wave
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pyaudio

from jarvis_log_client import JarvisLogger

from core.audio_bus import AudioBus
from utils.config_service import Config
from utils.encryption_utils import get_cache_dir


@dataclass
class RecordingResult:
    """Metadata from a recording session."""
    audio_file: str          # path to the WAV file
    duration: float          # actual recording duration in seconds
    hit_max_duration: bool   # True if recording stopped due to max_record_seconds


logger = JarvisLogger(service="jarvis-node")
_cache_dir = get_cache_dir()


def _audio_defaults() -> dict:
    """Live read of audio-related config values."""
    return {
        "silence_threshold": Config.get_int("silence_threshold", 300),
        "silence_duration": Config.get_float("silence_duration", 1.5),
        "min_record_seconds": Config.get_float("min_record_seconds", 2.0),
        "max_record_seconds": Config.get_int("max_record_seconds", 7),
    }


def calculate_rms(audio_data: bytes) -> float:
    """RMS of a 16-bit PCM chunk. Returns 0.0 on empty or NaN."""
    if not audio_data:
        return 0.0
    audio_array = np.frombuffer(audio_data, dtype=np.int16)
    if len(audio_array) == 0:
        return 0.0
    rms = np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))
    if np.isnan(rms):
        return 0.0
    return float(rms)


def _write_wav(path: str, frames: List[bytes], bus: AudioBus) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(bus.channels)
        wf.setsampwidth(pyaudio.get_sample_size(bus.sample_format))
        wf.setframerate(bus.rate)
        wf.writeframes(b"".join(frames))


def listen(
    bus: AudioBus,
    *,
    subscriber_name: str = "listen",
    history_secs: float = 0.0,
    skip_secs: float = 0.3,
    silence_threshold: Optional[int] = None,
    silence_duration: Optional[float] = None,
    min_record_secs: Optional[float] = None,
    max_record_secs: Optional[float] = None,
) -> RecordingResult:
    """Record a command from the bus until end-of-speech.

    Subscribes to ``bus`` with ``history_secs`` of pre-buffered audio,
    reads chunks, tracks consecutive-silence frames, and stops when the
    silence window has been exceeded AND the minimum record length has
    been reached. Writes the captured audio to ``command.wav`` and
    returns metadata.

    All four timing knobs fall back to live Config if not overridden —
    so the state machine can pass longer silence windows for command
    capture without hard-coding them.
    """
    defaults = _audio_defaults()
    silence_threshold = silence_threshold if silence_threshold is not None else defaults["silence_threshold"]
    silence_duration = silence_duration if silence_duration is not None else defaults["silence_duration"]
    min_record_secs = min_record_secs if min_record_secs is not None else defaults["min_record_seconds"]
    max_record_secs = max_record_secs if max_record_secs is not None else defaults["max_record_seconds"]

    output_filename = str(_cache_dir / "command.wav")
    chunk_secs = bus.chunk_samples / bus.rate
    silence_frames_threshold = max(1, int(silence_duration / chunk_secs))
    min_frames = max(1, int(min_record_secs / chunk_secs))
    max_frames = max(min_frames, int(max_record_secs / chunk_secs))

    logger.info(
        "Listening for speech",
        history_secs=history_secs,
        silence_threshold=silence_threshold,
        silence_duration=silence_duration,
        min_seconds=min_record_secs,
        max_seconds=max_record_secs,
    )

    q = bus.subscribe(subscriber_name, history_secs=history_secs)
    frames: List[bytes] = []
    silence_frames = 0
    hit_max = False
    # Discard the first ``skip_secs`` worth of chunks to dodge TTS tail
    # bleed / AEC recovery after the wake-response playback. Without this,
    # the mic still captures residual speaker output as "audio", Whisper
    # transcribes it, and the node ends up responding to itself.
    skip_chunks = max(0, int(skip_secs / chunk_secs)) if skip_secs > 0 else 0
    try:
        for _ in range(skip_chunks):
            try:
                q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                break

        for frame_count in range(max_frames):
            try:
                data = q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                logger.warning("listen() timeout waiting for audio chunk")
                break

            frames.append(data)
            rms = calculate_rms(data)

            if rms < silence_threshold:
                silence_frames += 1
            else:
                silence_frames = 0

            if silence_frames >= silence_frames_threshold and frame_count >= min_frames:
                logger.debug("Silence detected, stopping recording", silence_duration=silence_duration)
                break

            if frame_count % 50 == 0:
                elapsed = frame_count * chunk_secs
                logger.debug(
                    "Recording progress",
                    elapsed=f"{elapsed:.1f}s",
                    rms=f"{rms:.0f}",
                    silence_frames=silence_frames,
                    silence_threshold_frames=silence_frames_threshold,
                )
        else:
            hit_max = True
    finally:
        bus.unsubscribe(subscriber_name)

    actual_duration = len(frames) * chunk_secs
    logger.info("Recording complete", duration=f"{actual_duration:.2f}s", hit_max=hit_max)

    _write_wav(output_filename, frames, bus)
    return RecordingResult(output_filename, actual_duration, hit_max)


def record_fixed_duration(
    bus: AudioBus,
    seconds: float,
    output_path: str,
    *,
    subscriber_name: str = "fixed_record",
    skip_secs: float = 0.0,
) -> RecordingResult:
    """Record exactly ``seconds`` of audio from the bus and write a WAV.

    Unlike ``listen()``, this ignores silence — useful for voice-profile
    enrollment where we want a fixed-length sample regardless of pauses.
    """
    chunk_secs = bus.chunk_samples / bus.rate
    skip_chunks = max(0, int(skip_secs / chunk_secs)) if skip_secs > 0 else 0
    record_chunks = max(1, int(seconds / chunk_secs))

    logger.info(
        "Fixed-duration recording starting",
        seconds=seconds,
        skip_secs=skip_secs,
        output=output_path,
    )

    q = bus.subscribe(subscriber_name)
    frames: List[bytes] = []
    try:
        for _ in range(skip_chunks):
            try:
                q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                break
        for _ in range(record_chunks):
            try:
                data = q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                logger.warning("record_fixed_duration: timeout waiting for chunk")
                break
            frames.append(data)
    finally:
        bus.unsubscribe(subscriber_name)

    actual_duration = len(frames) * chunk_secs
    logger.info("Fixed-duration recording complete", duration=f"{actual_duration:.2f}s")
    _write_wav(output_path, frames, bus)
    return RecordingResult(output_path, actual_duration, hit_max_duration=True)


def listen_for_follow_up(
    bus: AudioBus,
    *,
    subscriber_name: str = "follow_up",
    timeout_seconds: float = 5.0,
    history_secs: float = 0.0,
    silence_threshold: Optional[int] = None,
    silence_duration: Optional[float] = None,
    max_record_secs: Optional[float] = None,
) -> str | None:
    """Listen for follow-up speech within a timeout window.

    Subscribes to the bus, waits up to ``timeout_seconds`` for speech
    onset (3 consecutive frames with RMS >= silence_threshold). If
    detected, switches to normal recording mode until silence. If the
    timeout expires without speech, returns None.

    Defaults ``history_secs=0`` — follow-up cares about NEW speech, not
    the tail of the preceding TTS.
    """
    defaults = _audio_defaults()
    silence_threshold = silence_threshold if silence_threshold is not None else defaults["silence_threshold"]
    silence_duration = silence_duration if silence_duration is not None else defaults["silence_duration"]
    max_record_secs = max_record_secs if max_record_secs is not None else defaults["max_record_seconds"]

    output_filename = str(_cache_dir / "follow_up.wav")
    chunk_secs = bus.chunk_samples / bus.rate
    silence_frames_threshold = max(1, int(silence_duration / chunk_secs))
    max_frames = max(1, int(max_record_secs / chunk_secs))
    onset_required = 3

    logger.debug("Follow-up listening window opened", timeout_seconds=timeout_seconds)

    q = bus.subscribe(subscriber_name, history_secs=history_secs)
    frames: List[bytes] = []
    speech_detected = False
    onset_count = 0
    onset_deadline = time.monotonic() + timeout_seconds
    try:
        while time.monotonic() < onset_deadline:
            remaining = max(0.05, onset_deadline - time.monotonic())
            try:
                data = q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue

            rms = calculate_rms(data)
            if rms >= silence_threshold:
                onset_count += 1
                frames.append(data)
                if onset_count >= onset_required:
                    speech_detected = True
                    logger.info("Follow-up speech detected", rms=f"{rms:.0f}")
                    break
            else:
                onset_count = 0
                frames.clear()

        if not speech_detected:
            logger.debug("No follow-up speech detected, timeout expired")
            return None

        silence_frames = 0
        for _ in range(max_frames):
            try:
                data = q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                logger.warning("listen_for_follow_up timeout waiting for chunk")
                break

            frames.append(data)
            rms = calculate_rms(data)
            if rms < silence_threshold:
                silence_frames += 1
            else:
                silence_frames = 0

            if silence_frames >= silence_frames_threshold:
                logger.debug("Follow-up recording: silence detected, stopping")
                break
    finally:
        bus.unsubscribe(subscriber_name)

    actual_duration = len(frames) * chunk_secs
    logger.info("Follow-up recording complete", duration=f"{actual_duration:.2f}s")
    _write_wav(output_filename, frames, bus)
    return output_filename
