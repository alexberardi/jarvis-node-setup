import os
import json
import subprocess

CONFIG_PATH = os.path.expanduser("~/projects/jarvis-node-setup/config.json")

with open(CONFIG_PATH) as f:
    config = json.load(f)


def speak(text):
    print(f"Speaking: {text}")
    subprocess.run(["espeak", "--v", "en-uk", text], stderr=subprocess.DEVNULL)

if __name__ == "__main__":
    speak("Hello! I am ready.")
