from jarvis_log_client import JarvisLogger
from core.helpers import get_tts_provider

logger = JarvisLogger(service="jarvis-node")


def speak(text: str):
    logger.info("Speaking", text=text)
    tts_provider = get_tts_provider()
    tts_provider.speak(False, text)



if __name__ == "__main__":
    speak("Hello! I am ready.")
