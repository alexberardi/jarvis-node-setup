import pyaudio
import wave
import os
import json

CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

with open(CONFIG_PATH) as f:
    config = json.load(f)

mic_sample_rate = int(config.get("mic_sample_rate", 48000))
mic_channels = 1
mic_device_index = int(config.get("mic_device_index", 1))
frames_per_buffer = int(mic_sample_rate * 0.032) # 32ms

RECORD_SECONDS = 5
OUTPUT_FILENAME = "/tmp/command.wav"

def listen_and_transcribe():
    print("🎙️ Listening for speech...")

    audio = pyaudio.PyAudio()

    stream = audio.open(
            format=pyaudio.paInt16,
            channels=mic_channels,
            rate=mic_sample_rate,
            input=True,
            input_device_index=mic_device_index,
            frames_per_buffer=frames_per_buffer
            )

    frames = []

    for _ in range(0, int(mic_sample_rate / frames_per_buffer * RECORD_SECONDS)):
        data = stream.read(frames_per_buffer, exception_on_overflow=False)
        frames.append(data)

    print("🛑 Recording complete.")

    stream.stop_stream()
    stream.close()
    audio.terminate()

    # Save to WAV
    with wave.open(OUTPUT_FILENAME, 'wb') as wf:
        wf.setnchannels(mic_channels)
        wf.setsampwidth(audio.get_sample_size(pyaudio.paInt16))
        wf.setframerate(mic_sample_rate)
        wf.writeframes(b''.join(frames))

    # 🔁 You can plug in Whisper, SpeechRecognition, etc. here:
    # For now, return the file path
    return OUTPUT_FILENAME


