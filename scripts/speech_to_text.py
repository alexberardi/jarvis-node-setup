import pyaudio
import wave
import os
import json
from utils.config_loader import Config


mic_sample_rate = int(Config.get("mic_sample_rate", 48000))
mic_channels = 1
mic_device_index = int(Config.get("mic_device_index", 1))
frames_per_buffer = int(mic_sample_rate * 0.032)  # 32ms

RECORD_SECONDS = 5
OUTPUT_FILENAME = "/tmp/command.wav"


def listen():
    print("🎙️ Listening for speech...")

    audio = pyaudio.PyAudio()

    stream = audio.open(
        format=pyaudio.paInt16,
        channels=mic_channels,
        rate=mic_sample_rate,
        input=True,
        input_device_index=mic_device_index,
        frames_per_buffer=frames_per_buffer,
    )

    frames = []

    upper_range = int(mic_sample_rate / frames_per_buffer * RECORD_SECONDS)
    for _ in range(0, upper_range):
        data = stream.read(frames_per_buffer, exception_on_overflow=False)
        frames.append(data)

    print("🛑 Recording complete.")

    stream.stop_stream()
    stream.close()
    audio.terminate()

    # Save to WAV
    with wave.open(OUTPUT_FILENAME, "wb") as wf:
        wf.setnchannels(mic_channels)
        wf.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
        wf.setframerate(mic_sample_rate)
        wf.writeframes(b"".join(frames))

    # 🔁 You can plug in Whisper, SpeechRecognition, etc. here:
    # For now, return the file path
    return OUTPUT_FILENAME
