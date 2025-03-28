import os
import json
import subprocess

CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

with open(CONFIG_PATH) as f:
    config = json.load(f)


def speak(text):
    print(f"Speaking: {text}")
    subprocess.run(
        f'espeak -a 20 -v en-uk -s 140 "{text}" --stdout | aplay -r 44100',
        shell=True,
    )


if __name__ == "__main__":
    speak("Hello! I am ready.")
