import pvporcupine
import pyaudio
import struct
import subprocess
import voice_sender
import os
import json

CHIME_PATH = "/home/pi/projects/jarvis-node-setup/sounds/chime.wav"
CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

with open(CONFIG_PATH) as f:
    config = json.load(f)

PORCUPINE_KEY = config["porcupine_key"]

def play_chime():
    subprocess.run(["aplay", CHIME_PATH])

def simulate_transcribe():
    # Stub — replace with real transcription later
    return "Turn on the hallway lights"

def main():
    print("👂 Starting Porcupine wake word engine...")
    porcupine = pvporcupine.create(access_key=PORCUPINE_KEY,keywords=["jarvis"])

    pa = pyaudio.PyAudio()
    audio_stream = pa.open(
        rate=porcupine.sample_rate,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=porcupine.frame_length
    )

    print("🟢 Listening for 'Jarvis'...")

    try:
        while True:
            pcm = audio_stream.read(porcupine.frame_length, exception_on_overflow=False)
            pcm = struct.unpack_from("h" * porcupine.frame_length, pcm)

            if porcupine.process(pcm) >= 0:
                print("🎤 Wake word detected!")
                play_chime()

                # Placeholder — replace with real audio + transcription later
                text = simulate_transcribe()
                print(f"🗣️ Command: {text}")
                voice_sender.send_voice(text)

    except KeyboardInterrupt:
        print("🔴 Stopped by user.")
    finally:
        audio_stream.stop_stream()
        audio_stream.close()
        pa.terminate()
        porcupine.delete()

if __name__ == "__main__":
    main()

