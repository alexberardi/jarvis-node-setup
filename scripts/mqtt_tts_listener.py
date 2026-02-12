import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from jarvis_log_client import JarvisLogger

from utils.config_service import Config
from core.helpers import get_tts_provider
from utils.music_assistant_service import MusicAssistantService

logger = JarvisLogger(service="jarvis-node")

REPO_ROOT: Path = Path(__file__).resolve().parents[1]

# Guard: only one training process at a time
_training_lock = threading.Lock()
_training_running = False


def get_mqtt_config() -> Dict[str, Any]:
    """Get MQTT configuration at runtime"""
    node_id: str = Config.get_str("node_id", "unknown") or "unknown"
    return {
        "topic": Config.get_str("mqtt_topic", f"jarvis/nodes/{node_id}/#") or f"jarvis/nodes/{node_id}/#",
        "broker": Config.get_str("mqtt_broker", "raspberrypi.local") or "raspberrypi.local",
        "port": Config.get_int("mqtt_port", 1884) or 1884,
        "username": Config.get_str("mqtt_username", "") or "",
        "password": Config.get_str("mqtt_password", "") or ""
    }


def handle_tts(details: Dict[str, Any]) -> None:
    message: str = details.get("message", "")
    tts_provider = get_tts_provider()
    tts_provider.speak(True, message)


def _verify_command(request_id: str) -> bool:
    """Verify a command request_id with the command center."""
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.error("Cannot verify command: command center URL not resolved")
        return False

    url = f"{base_url.rstrip('/')}/api/v0/commands/{request_id}/verify"
    result: Optional[Dict[str, Any]] = RestClient.post(url, data={}, timeout=10)
    if result and result.get("valid"):
        return True
    logger.warning("Command verification failed", request_id=request_id[:8])
    return False


def _run_training() -> None:
    """Run train_node_adapter.py in a subprocess."""
    global _training_running
    try:
        logger.info("Starting adapter training subprocess")
        subprocess.run(
            ["python", "scripts/train_node_adapter.py"],
            cwd=str(REPO_ROOT),
            check=True,
        )
        logger.info("Adapter training completed successfully")
    except subprocess.CalledProcessError as e:
        logger.error("Adapter training failed", return_code=e.returncode)
    except Exception as e:
        logger.error("Adapter training error", error=str(e))
    finally:
        with _training_lock:
            _training_running = False


def handle_train_adapter(details: Dict[str, Any]) -> None:
    """Verify and trigger adapter training in a background thread."""
    global _training_running

    request_id: Optional[str] = details.get("request_id")
    if not request_id:
        logger.warning("train_adapter: missing request_id, ignoring")
        return

    # Only one training at a time
    with _training_lock:
        if _training_running:
            logger.warning("train_adapter: training already in progress, ignoring")
            return
        _training_running = True

    # Verify the command before executing
    if not _verify_command(request_id):
        with _training_lock:
            _training_running = False
        return

    thread = threading.Thread(target=_run_training, daemon=True)
    thread.start()
    logger.info("Adapter training thread started", request_id=request_id[:8])


command_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "tts": handle_tts,
    "train_adapter": handle_train_adapter,
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
