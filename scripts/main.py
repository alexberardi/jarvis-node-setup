import threading

from scripts.mqtt_tts_listener import start_mqtt_listener
from scripts.voice_listener import start_voice_listener
from services.timer_service import initialize_timer_service
from utils.config_service import Config
from utils.music_assistant_service import DummyMusicAssistantService, MusicAssistantService
from utils.service_discovery import init as init_service_discovery


def main():
    # Initialize service discovery (config service → JSON config fallback)
    if init_service_discovery():
        print("[Jarvis] Service discovery initialized")
    else:
        print("[Jarvis] Using JSON config for service URLs")

    # Initialize timer service with TTS callback
    timer_service = initialize_timer_service()

    # Restore any persisted timers from previous session
    restored_count = timer_service.restore_timers()
    if restored_count > 0:
        print(f"[Jarvis] Restored {restored_count} timer(s) from previous session")

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

