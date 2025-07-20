import json
import paho.mqtt.client as mqtt
from typing import Any, Dict, List, Optional, Callable
from utils.config_service import Config
from core.helpers import get_tts_provider
from utils.music_assistant_service import MusicAssistantService


def get_mqtt_config() -> Dict[str, Any]:
    """Get MQTT configuration at runtime"""
    return {
        "topic": Config.get_str("mqtt_topic", "home/nodes/zero-office/#") or "home/nodes/zero-office/#",
        "broker": Config.get_str("mqtt_broker", "raspberrypi.local") or "raspberrypi.local",
        "port": Config.get_int("mqtt_port", 1884) or 1884,
        "username": Config.get_str("mqtt_username", "") or "",
        "password": Config.get_str("mqtt_password", "") or ""
    }


def handle_tts(details: Dict[str, Any]) -> None:
    message: str = details.get("message", "")
    tts_provider = get_tts_provider()
    tts_provider.speak(True, message)


command_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "tts": handle_tts
}


def on_connect(client: mqtt.Client, userdata: Any, flags: Dict[str, int], rc: int) -> None:
    print(f"[MQTT] Connected with result code {rc}")
    topic = get_mqtt_config()["topic"]
    client.subscribe(topic)
    print(f"[MQTT] Subscribed to topic: {topic}")


def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    try:
        payload: List[Dict[str, Any]] = json.loads(msg.payload.decode())
        print(f"[MQTT] Received message: {payload}")
    except json.JSONDecodeError:
        print("⚠️ Invalid JSON payload")
        return
    except Exception as e:
        print(f"[ERROR] {e}")
        return

    if not isinstance(payload, list):
        print("⚠️ Payload is not a list of commands")
        return

    for command_obj in payload:
        command: str = command_obj.get("command", "")
        details: Dict[str, Any] = command_obj.get("details", {})

        handler: Optional[Callable[[Dict[str, Any]], None]] = command_handlers.get(command)

        if handler:
            try:
                handler(details)
            except Exception as e:
                print(f"❌ Error running handler for '{command}': {e}")
        else:
            print(f"⚠️ Unknown command: {command}")


def start_mqtt_listener(ma_service: MusicAssistantService) -> None:
    config = get_mqtt_config()
    client: mqtt.Client = mqtt.Client()

    if config["username"] and config["password"]:
        client.username_pw_set(config["username"], config["password"])

    client.on_connect = on_connect
    client.on_message = on_message

    print("📡 MQTT listener started...")
    client.connect(config["broker"], config["port"], 60)
    client.loop_forever()
