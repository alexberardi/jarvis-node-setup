import os
import queue
import sounddevice as sd
from vosk import Model, KaldiRecognizer

model_path = os.path.expanduser("~/projects/jarvis-node-setup/models/en")
model = Model(model_path)
rec = KaldiRecognizer(model, 16000)
audio_queue = queue.Queue()

def callback(indata, frames, time, status):
    if status:
        print(f"[Audio Status] {status}")
    audio_queue.put(bytes(indata))

def listen_and_transcribe():
    with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype='int16', channels=1, callback=callback):
        print("Listening...")
        while True:
            data = audio_queue.get()
            if rec.AcceptWaveform(data):
                result = rec.Result()
                print(result)
                return result
            else:
                print(rec.PartialResult())

if __name__ == "__main__":
    listen_and_transcribe()
