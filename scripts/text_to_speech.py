import subprocess
from core.helpers import get_tts_provider


def speak(text: str):
    print(f"Speaking: {text}")
    tts_provider = get_tts_provider()
    tts_provider.speak(False, text)



if __name__ == "__main__":
    speak("Hello! I am ready.")
