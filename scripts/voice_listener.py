import numpy as np
import pvporcupine
import pyaudio
from scipy.signal import resample
import time

from scripts.speech_to_text import listen
from scripts.text_to_speech import speak
from clients.jarvis_whisper_client import JarvisWhisperClient
from utils.config_loader import Config

CHIME_PATH = "/home/pi/projects/jarvis-node-setup/sounds/chime.wav"


PORCUPINE_KEY = Config.get("porcupine_key", "")
MIC_SAMPLE_RATE = Config.get("mic_sample_rate", 48000)
FRAMES_PER_BUFFER = int(MIC_SAMPLE_RATE * 0.032)  # ~32ms chunk

# This is setup through the setup.sh script but if you have a different
# default mic index, set it through config
MIC_DEVICE_INDEX = Config.get("mic_device_index", 1)


def create_audio_stream():
    return pa.open(
        rate=MIC_SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=FRAMES_PER_BUFFER,
        input_device_index=MIC_DEVICE_INDEX,
    )


def handle_keyword_detected():
    print("🟢 Wake word detected! Listening for command...")
    speak("Yes?")


def close_audio():
    audio_stream.stop_stream()
    audio_stream.close()
    pa.terminate()


def send_for_transcription(filename):
    print("📡 Sending to transcription server...")
    response = JarvisWhisperClient.transcribe(filename)
    if response is not None:
        print("📝 Transcription:", response["text"])
        speak(response["text"])
    else:
        speak("An error occurred")


porcupine = pvporcupine.create(access_key=PORCUPINE_KEY, keywords=["jarvis"])
pa = pyaudio.PyAudio()
audio_stream = create_audio_stream()

print("👂 Waiting for wake word...")

try:
    while True:
        raw_data = audio_stream.read(
            audio_stream._frames_per_buffer, exception_on_overflow=False
        )
        samples = np.frombuffer(raw_data, dtype=np.int16)

        # Resample from 48000 → 16000 Hz
        resampled = resample(samples, porcupine.frame_length).astype(np.int16)

        keyword_index = porcupine.process(resampled.tolist())
        if keyword_index >= 0:
            handle_keyword_detected()

            close_audio()

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
