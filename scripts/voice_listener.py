import pvporcupine
import pyaudio
import struct
import subprocess
import voice_sender
import os
import json
import numpy as np
from scipy.signal import resample
from speech_to_text import listen_and_transcribe
from text_to_speech import speak

CHIME_PATH = "/home/pi/projects/jarvis-node-setup/sounds/chime.wav"
CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

with open(CONFIG_PATH) as f:
    config = json.load(f)


access_key = config["porcupine_key"]
mic_sample_rate = config.get("mic_sample_rate", 48000)
# The setup script forces USB mics to always be card 1, if your setup is different, you may have to add a config entry and pull it in here as a variable
porcupine = pvporcupine.create(access_key=access_key, keywords=["jarvis"])
pa = pyaudio.PyAudio()
audio_stream = pa.open(
        rate=mic_sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=int(mic_sample_rate / 100)
        )

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
            result = listen_and_transcribe()
            print("📝 Transcription result:", result)
except KeyboardInterrupt:
    print("Stopping...")
finally:
    audio_stream.stop_stream()
    audio_stream.close()
    pa.terminate()
    porcupine.delete()

