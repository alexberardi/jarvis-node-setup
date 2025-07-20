import threading
from utils.config_service import Config
from scripts.voice_listener import start_voice_listener
from scripts.mqtt_tts_listener import start_mqtt_listener
from utils.music_assistant_service import MusicAssistantService, DummyMusicAssistantService


def main():
    if Config.get("music_assistant_enabled"):
        ma_service = MusicAssistantService()
    else:
        ma_service = DummyMusicAssistantService()

    # Start MQTT listener in thread
    mqtt_thread = threading.Thread(target=start_mqtt_listener, args=(ma_service,), daemon=True)
    mqtt_thread.start()

    # Start voice listener in main thread
    start_voice_listener(ma_service)


if __name__ == "__main__":
    main()

