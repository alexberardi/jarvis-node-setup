import pyaudio
import wave
import os
import json
import numpy as np
from typing import List, Any
from utils.config_service import Config


def get_audio_config() -> dict:
    """Get audio configuration at runtime"""
    sample_rate = Config.get_int("mic_sample_rate", 48000)
    return {
        "sample_rate": sample_rate,
        "channels": 1,
        "device_index": Config.get_int("mic_device_index", 1),
        "frames_per_buffer": int(sample_rate * 0.032),  # 32ms
        "max_record_seconds": Config.get_int("max_record_seconds", 7),
        "silence_threshold": Config.get_int("silence_threshold", 500),  # RMS threshold for silence
        "silence_duration": Config.get_float("silence_duration", 1.0),  # Seconds of silence to trigger stop
        "min_record_seconds": Config.get_float("min_record_seconds", 0.5)  # Minimum recording time
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
    print("🎙️ Listening for speech...")
    
    config = get_audio_config()
    OUTPUT_FILENAME: str = "/tmp/command.wav"
    
    audio: pyaudio.PyAudio = pyaudio.PyAudio()

    stream: pyaudio.Stream = audio.open(
        format=pyaudio.paInt16,
        channels=config["channels"],
        rate=config["sample_rate"],
        input=True,
        input_device_index=config["device_index"],
        frames_per_buffer=config["frames_per_buffer"],
    )

    frames: List[bytes] = []
    silence_frames: int = 0
    silence_threshold_frames: int = int(config["silence_duration"] * config["sample_rate"] / config["frames_per_buffer"])
    min_record_frames: int = int(config["min_record_seconds"] * config["sample_rate"] / config["frames_per_buffer"])
    max_record_frames: int = int(config["max_record_seconds"] * config["sample_rate"] / config["frames_per_buffer"])
    
    print(f"🔊 Silence threshold: {config['silence_threshold']} RMS, Duration: {config['silence_duration']}s")
    print(f"⏱️ Min: {config['min_record_seconds']}s, Max: {config['max_record_seconds']}s")

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
            print(f"🔇 Detected {config['silence_duration']}s of silence, stopping recording")
            break
        
        # Optional: Print progress for debugging
        if frame_count % 50 == 0:  # Every ~1.6 seconds at 48kHz
            elapsed = frame_count * config["frames_per_buffer"] / config["sample_rate"]
            print(f"⏱️ Recording: {elapsed:.1f}s, RMS: {rms:.0f}, Silence: {silence_frames}/{silence_threshold_frames}")

    actual_duration = len(frames) * config["frames_per_buffer"] / config["sample_rate"]
    print(f"🛑 Recording complete. Duration: {actual_duration:.2f}s")

    stream.stop_stream()
    stream.close()
    audio.terminate()

    # Save to WAV
    with wave.open(OUTPUT_FILENAME, "wb") as wf:
        wf.setnchannels(config["channels"])
        wf.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
        wf.setframerate(config["sample_rate"])
        wf.writeframes(b"".join(frames))

    return OUTPUT_FILENAME
