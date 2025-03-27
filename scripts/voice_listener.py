import pvporcupine
import pyaudio
import struct
import subprocess
import voice_sender
import os
import json
import requests
import numpy as np
from scipy.signal import resample
from speech_to_text import listen
from text_to_speech import speak
import time
import os

CHIME_PATH = "/home/pi/projects/jarvis-node-setup/sounds/chime.wav"
CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

with open(CONFIG_PATH) as f:
    config = json.load(f)


access_key = config["porcupine_key"]
mic_sample_rate = config.get("mic_sample_rate", 48000)
frames_per_buffer = int(mic_sample_rate * 0.032) # ~32ms chunk
mic_device_index = 1 # this is setup through the setup.sh script
API_URL = config.get("api_url", "http://10.0.0.173:9999")


def create_audio_stream():
    return pa.open(
        rate=mic_sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=frames_per_buffer,
        input_device_index=mic_device_index
        )

def send_for_transcription(filename):
    with open(filename, 'rb') as f:
        files = {'file': ('command.wav', f, 'audio/wav') }
        try:
            print("📡 Sending to transcription server...")
            response = requests.post(f"{API_URL}/transcribe", files=files, timeout=30)
            command = response.json().get("text", "No text found")
            print("📝 Transcription:", command)
            return command
        except Exception as e:
            print("❌ Transcription failed:", e)



# The setup script forces USB mics to always be card 1, if your setup is different, you may have to add a config entry and pull it in here as a variable
porcupine = pvporcupine.create(access_key=access_key, keywords=["jarvis"])
pa = pyaudio.PyAudio()
audio_stream = create_audio_stream()
print("👂 Waiting for wake word...")

try: 
    while True:
        raw_data = audio_stream.read(audio_stream._frames_per_buffer, exception_on_overflow=False)
        samples = np.frombuffer(raw_data, dtype=np.int16)

        # Resample from 48000 → 16000 Hz
        resampled = resample(samples, porcupine.frame_length).astype(np.int16)

        keyword_index = porcupine.process(resampled.tolist())
        if keyword_index >= 0:
            print("🟢 Wake word detected! Listening for command...")
            speak("Yes?")

            audio_stream.stop_stream()
            audio_stream.close()
            pa.terminate()

            audio_file = listen()

            start = time.perf_counter()
            command = send_for_transcription(audio_file)
            end = time.perf_counter()

            print(f"⏱️ Transcription took {end - start:.2f} seconds")
            

            # reinitialize porcupine + pyaudio
            pa = pyaudio.PyAudio()
            audio_stream = create_audio_stream()

except KeyboardInterrupt:
    print("Stopping...")
finally:
    audio_stream.stop_stream()
    audio_stream.close()
    pa.terminate()
    porcupine.delete()

