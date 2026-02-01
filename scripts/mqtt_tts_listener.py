import json
import paho.mqtt.client as mqtt
from typing import Any, Dict, List, Optional, Callable

from jarvis_log_client import JarvisLogger

from utils.config_service import Config
from core.helpers import get_tts_provider
from utils.music_assistant_service import MusicAssistantService

logger = JarvisLogger(service="jarvis-node")


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
    logger.info("MQTT connected", result_code=rc)
    topic = get_mqtt_config()["topic"]
    client.subscribe(topic)
    logger.info("MQTT subscribed", topic=topic)


def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    try:
        payload: List[Dict[str, Any]] = json.loads(msg.payload.decode())
        logger.debug("MQTT message received", payload=payload)
    except json.JSONDecodeError:
        logger.warning("Invalid JSON payload in MQTT message")
        return
    except Exception as e:
        logger.error("Error processing MQTT message", error=str(e))
        return

    if not isinstance(payload, list):
        logger.warning("MQTT payload is not a list of commands")
        return

    for command_obj in payload:
        command: str = command_obj.get("command", "")
        details: Dict[str, Any] = command_obj.get("details", {})

        handler: Optional[Callable[[Dict[str, Any]], None]] = command_handlers.get(command)

        if handler:
            try:
                handler(details)
            except Exception as e:
                logger.error("Error running MQTT handler", command=command, error=str(e))
        else:
            logger.warning("Unknown MQTT command", command=command)


def start_mqtt_listener(ma_service: MusicAssistantService) -> None:
    config = get_mqtt_config()
    client: mqtt.Client = mqtt.Client()

    if config["username"] and config["password"]:
        client.username_pw_set(config["username"], config["password"])

    client.on_connect = on_connect
    client.on_message = on_message

    logger.info("MQTT listener starting", broker=config["broker"], port=config["port"])
    client.connect(config["broker"], config["port"], 60)
    client.loop_forever()
