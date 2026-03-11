import json
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

from jarvis_log_client import JarvisLogger

from utils.config_service import Config
from core.helpers import get_tts_provider
from services.config_push_service import process_pending_configs
from services.settings_snapshot_service import handle_snapshot_request
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

    # Subscribe to auth-ready notifications from JCC OAuth flow
    client.subscribe("jarvis/auth/+/ready")
    logger.info("MQTT subscribed", topic="jarvis/auth/+/ready")


def _handle_auth_ready(raw_payload: bytes) -> None:
    """Handle OAuth auth-ready notification from JCC.

    JCC publishes this after a successful OAuth callback.
    Node pulls credentials from JCC and stores them via the command's store_auth_values().
    """
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in auth-ready notification")
        return

    provider: str = notification.get("provider", "")
    node_id_from_msg: str = notification.get("node_id", "")
    my_node_id: str = Config.get_str("node_id", "") or ""

    # Only process if this notification is for us
    if node_id_from_msg != my_node_id:
        return

    logger.info("Auth ready notification received", provider=provider)
    thread = threading.Thread(target=_pull_auth_credentials, args=(provider,), daemon=True)
    thread.start()


def _pull_auth_credentials(provider: str) -> None:
    """Pull OAuth credentials from JCC and store them locally."""
    from clients.rest_client import RestClient
    from utils.command_discovery_service import get_command_discovery_service
    from utils.service_discovery import get_command_center_url

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.error("Cannot pull auth credentials: command center URL not resolved")
        return

    url = f"{base_url.rstrip('/')}/api/v0/oauth/provider/{provider}/credentials"
    result: Optional[Dict[str, Any]] = RestClient.get(url, timeout=15)
    if result is None:
        logger.error("Failed to pull auth credentials", provider=provider)
        return

    # Map JCC response keys to what store_auth_values() expects
    # JCC returns "base_url", commands expect "_base_url"
    if "base_url" in result and "_base_url" not in result:
        result["_base_url"] = result["base_url"]

    # Find the command that owns this provider and store credentials
    service = get_command_discovery_service()
    commands = service.get_all_commands()

    for cmd in commands.values():
        if cmd.authentication and cmd.authentication.provider == provider:
            logger.info("Storing auth credentials", provider=provider, command=cmd.command_name)
            cmd.store_auth_values(result)
            return

    logger.warning("No command found for auth provider", provider=provider)


def _handle_config_push_notification(raw_payload: bytes) -> None:
    """Handle config push MQTT notification — triggers polling in background."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in config push notification")
        return

    config_type: str = notification.get("config_type", "unknown")
    logger.info("Config push notification received", config_type=config_type)

    thread = threading.Thread(target=_process_config_push, daemon=True)
    thread.start()


def _process_config_push() -> None:
    """Process pending config pushes (runs in background thread)."""
    try:
        count: int = process_pending_configs()
        logger.info("Config push processing complete", processed=count)
    except Exception as e:
        logger.error("Config push processing failed", error=str(e))


def _handle_settings_request_notification(raw_payload: bytes) -> None:
    """Handle settings snapshot request MQTT notification."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in settings request notification")
        return

    request_id: str = notification.get("request_id", "")
    if not request_id:
        logger.warning("Settings request notification missing request_id")
        return

    logger.info("Settings snapshot requested", request_id=request_id[:8])
    thread = threading.Thread(target=_process_settings_request, args=(request_id,), daemon=True)
    thread.start()


def _process_settings_request(request_id: str) -> None:
    """Process a settings snapshot request (runs in background thread)."""
    try:
        success: bool = handle_snapshot_request(request_id)
        if success:
            logger.info("Settings snapshot complete", request_id=request_id[:8])
        else:
            logger.error("Settings snapshot failed", request_id=request_id[:8])
    except Exception as e:
        logger.error("Settings snapshot error", request_id=request_id[:8], error=str(e))


def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    # Route by topic — auth-ready notifications from JCC OAuth flow
    if msg.topic.startswith("jarvis/auth/") and msg.topic.endswith("/ready"):
        _handle_auth_ready(msg.payload)
        return

    # Route by topic suffix — config push notifications are plain objects, not arrays
    if msg.topic.endswith("/config/push"):
        _handle_config_push_notification(msg.payload)
        return

    if msg.topic.endswith("/settings/request"):
        _handle_settings_request_notification(msg.payload)
        return

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
    client: mqtt.Client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

    if config["username"] and config["password"]:
        client.username_pw_set(config["username"], config["password"])

    client.on_connect = on_connect
    client.on_message = on_message

    logger.info("MQTT listener starting", broker=config["broker"], port=config["port"])
    try:
        client.connect(config["broker"], config["port"], 60)
    except (ConnectionRefusedError, OSError) as e:
        logger.warning("MQTT broker not reachable, continuing without MQTT", broker=config["broker"], error=str(e))
        return
    client.loop_forever()
