import pvporcupine
import pyaudio
import struct
import subprocess
import voice_sender
import os
import json
from speech_to_text import listen_and_transcribe

CHIME_PATH = "/home/pi/projects/jarvis-node-setup/sounds/chime.wav"
CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

with open(CONFIG_PATH) as f:
    config = json.load(f)


access_key = config["porcupine_key"]
porcupine = pvporcupine.create(access_key=access_key, keywords=["jarvis"])
pa = pyaudio.PyAudio()
audio_stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length
        )

print("👂 Waiting for wake word...")

try: 
    while True:
        pcm = audio_stream.read(porcupine.frame_length, exception_on_overflow=False)
        pcm = [int.from_bytes(pcm[i:i+2], byteorder='little', signed=True) for i in range(0, len(pcm), 2)]
        keyword_index = porcupine.process(pcm)
        if keyword_index >= 0:
            print("🟢 Wake word detected! Listening for command...")
            listen_and_transcribe()
except KeyboardInterrupt:
    print("Stopping...")
finally:
    audio_stream.stop_stream()
    audio_stream.close()
    pa.terminate()
    porcupine.delete()

