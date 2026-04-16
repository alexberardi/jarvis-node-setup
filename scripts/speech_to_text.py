from typing import List

import numpy as np
import pyaudio
import wave

from jarvis_log_client import JarvisLogger
from utils.config_service import Config
from utils.encryption_utils import get_cache_dir

logger = JarvisLogger(service="jarvis-node")
_cache_dir = get_cache_dir()


def get_audio_config() -> dict:
    """Get audio configuration at runtime"""
    sample_rate = Config.get_int("mic_sample_rate", 48000)
    mic_index_str: str | None = Config.get_str("mic_device_index")
    device_index: int | None = int(mic_index_str) if mic_index_str is not None else None
    return {
        "sample_rate": sample_rate,
        "channels": 1,
        "device_index": device_index,
        "frames_per_buffer": int(sample_rate * 0.032),  # 32ms
        "max_record_seconds": Config.get_int("max_record_seconds", 7),
        "silence_threshold": Config.get_int("silence_threshold", 500),  # RMS threshold for silence
        "silence_duration": Config.get_float("silence_duration", 0.8),  # Seconds of silence to trigger stop
        "min_record_seconds": Config.get_float("min_record_seconds", 1.0)  # Minimum recording time
    }


def calculate_rms(audio_data: bytes) -> float:
    """Calculate RMS (Root Mean Square) of audio data to detect volume level"""
    # Handle empty data
    if not audio_data:
        return 0.0
    
    # Convert bytes to numpy array
    audio_array = np.frombuffer(audio_data, dtype=np.int16)
    
    # Handle empty array
    if len(audio_array) == 0:
        return 0.0
    
    # Calculate RMS
    rms = np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))
    
    # Convert numpy float to Python float and handle NaN
    if np.isnan(rms):
        return 0.0
    
    return float(rms)


def listen() -> str:
    logger.info("Listening for speech...")
    
    config = get_audio_config()
    output_filename: str = str(_cache_dir / "command.wav")

    audio: pyaudio.PyAudio = pyaudio.PyAudio()

    open_kwargs: dict = dict(
        format=pyaudio.paInt16,
        channels=config["channels"],
        rate=config["sample_rate"],
        input=True,
        frames_per_buffer=config["frames_per_buffer"],
    )
    if config["device_index"] is not None:
        open_kwargs["input_device_index"] = config["device_index"]
    stream: pyaudio.Stream = audio.open(**open_kwargs)

    frames: List[bytes] = []
    silence_frames: int = 0
    silence_threshold_frames: int = int(config["silence_duration"] * config["sample_rate"] / config["frames_per_buffer"])
    min_record_frames: int = int(config["min_record_seconds"] * config["sample_rate"] / config["frames_per_buffer"])
    max_record_frames: int = int(config["max_record_seconds"] * config["sample_rate"] / config["frames_per_buffer"])
    
    logger.debug("Audio config", silence_threshold=config['silence_threshold'], silence_duration=config['silence_duration'], min_seconds=config['min_record_seconds'], max_seconds=config['max_record_seconds'])

    for frame_count in range(max_record_frames):
        data: bytes = stream.read(config["frames_per_buffer"], exception_on_overflow=False)
        frames.append(data)
        
        # Calculate audio level
        rms = calculate_rms(data)
        
        # Check if this frame is silence
        if rms < config["silence_threshold"]:
            silence_frames += 1
        else:
            silence_frames = 0  # Reset silence counter when speech is detected
        
        # Stop if we've had enough silence and minimum recording time
        if (silence_frames >= silence_threshold_frames and 
            frame_count >= min_record_frames):
            logger.debug("Silence detected, stopping recording", silence_duration=config['silence_duration'])
            break
        
        # Optional: Print progress for debugging
        if frame_count % 50 == 0:  # Every ~1.6 seconds at 48kHz
            elapsed = frame_count * config["frames_per_buffer"] / config["sample_rate"]
            logger.debug("Recording progress", elapsed=f"{elapsed:.1f}s", rms=f"{rms:.0f}", silence_frames=silence_frames, silence_threshold_frames=silence_threshold_frames)

    actual_duration = len(frames) * config["frames_per_buffer"] / config["sample_rate"]
    logger.info("Recording complete", duration=f"{actual_duration:.2f}s")

    stream.stop_stream()
    stream.close()
    audio.terminate()

    # Save to WAV
    with wave.open(output_filename, "wb") as wf:
        wf.setnchannels(config["channels"])
        wf.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
        wf.setframerate(config["sample_rate"])
        wf.writeframes(b"".join(frames))

    return output_filename


def listen_for_follow_up(timeout_seconds: float = 5.0) -> str | None:
    """Listen for follow-up speech within a timeout window.

    Opens the mic and waits for speech onset. If speech is detected
    (RMS exceeds silence_threshold for 3+ consecutive frames), switches
    to normal recording mode (silence-for-1s = done, max 7s).

    Args:
        timeout_seconds: Max seconds to wait for speech onset.

    Returns:
        Path to WAV file if speech was captured, None if timeout expired.
    """
    config = get_audio_config()
    output_filename: str = str(_cache_dir / "follow_up.wav")

    # Skip first ~0.3s to avoid TTS bleed from speaker into mic
    skip_frames: int = int(0.3 * config["sample_rate"] / config["frames_per_buffer"])

    # 3 consecutive frames above threshold = speech onset (~96ms)
    speech_onset_required: int = 3
    speech_onset_count: int = 0

    timeout_frames: int = int(timeout_seconds * config["sample_rate"] / config["frames_per_buffer"])

    audio: pyaudio.PyAudio = pyaudio.PyAudio()

    open_kwargs: dict = dict(
        format=pyaudio.paInt16,
        channels=config["channels"],
        rate=config["sample_rate"],
        input=True,
        frames_per_buffer=config["frames_per_buffer"],
    )
    if config["device_index"] is not None:
        open_kwargs["input_device_index"] = config["device_index"]
    stream: pyaudio.Stream = audio.open(**open_kwargs)

    logger.debug("Follow-up listening window opened", timeout_seconds=timeout_seconds)

    frames: List[bytes] = []
    speech_detected: bool = False

    # Phase 1: Wait for speech onset
    for frame_idx in range(timeout_frames):
        data: bytes = stream.read(config["frames_per_buffer"], exception_on_overflow=False)

        # Skip initial frames (TTS bleed avoidance)
        if frame_idx < skip_frames:
            continue

        rms = calculate_rms(data)

        if rms >= config["silence_threshold"]:
            speech_onset_count += 1
            frames.append(data)  # Keep audio from potential speech start
            if speech_onset_count >= speech_onset_required:
                speech_detected = True
                logger.info("Follow-up speech detected", frame=frame_idx, rms=f"{rms:.0f}")
                break
        else:
            speech_onset_count = 0
            frames.clear()  # Discard non-speech audio

    if not speech_detected:
        logger.debug("No follow-up speech detected, timeout expired")
        stream.stop_stream()
        stream.close()
        audio.terminate()
        return None

    # Phase 2: Record until silence (same logic as listen())
    silence_frames: int = 0
    silence_threshold_frames: int = int(config["silence_duration"] * config["sample_rate"] / config["frames_per_buffer"])
    max_record_frames: int = int(config["max_record_seconds"] * config["sample_rate"] / config["frames_per_buffer"])

    for _ in range(max_record_frames):
        data = stream.read(config["frames_per_buffer"], exception_on_overflow=False)
        frames.append(data)

        rms = calculate_rms(data)
        if rms < config["silence_threshold"]:
            silence_frames += 1
        else:
            silence_frames = 0

        if silence_frames >= silence_threshold_frames:
            logger.debug("Follow-up recording: silence detected, stopping")
            break

    actual_duration = len(frames) * config["frames_per_buffer"] / config["sample_rate"]
    logger.info("Follow-up recording complete", duration=f"{actual_duration:.2f}s")

    stream.stop_stream()
    stream.close()

    # Save to WAV
    with wave.open(output_filename, "wb") as wf:
        wf.setnchannels(config["channels"])
        wf.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
        wf.setframerate(config["sample_rate"])
        wf.writeframes(b"".join(frames))

    audio.terminate()
    return output_filename
