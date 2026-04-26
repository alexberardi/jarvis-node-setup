import asyncio
import json
import os
import subprocess
import threading
import time
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

# Global MQTT client reference for publishing replies
_mqtt_client: Optional[mqtt.Client] = None

# Shutdown event shared with main.py for graceful shutdown
_shutdown_event: Optional[threading.Event] = None

# Module-level reference to tracked threads for heartbeat reporting
_tracked_threads: Optional[Dict[str, Any]] = None


def set_shutdown_event(event: threading.Event) -> None:
    """Accept a shutdown event from main.py for graceful shutdown."""
    global _shutdown_event
    _shutdown_event = event


def set_tracked_threads(threads: Dict[str, Any]) -> None:
    """Accept tracked threads dict from main.py for heartbeat status reporting."""
    global _tracked_threads
    _tracked_threads = threads


def _on_disconnect(client: mqtt.Client, userdata: Any, rc: int) -> None:
    """Handle MQTT disconnection. paho-mqtt will auto-reconnect via loop_forever()."""
    if rc == 0:
        logger.info("MQTT disconnected cleanly")
    else:
        logger.warning("MQTT disconnected unexpectedly, will auto-reconnect", reason_code=rc)


def get_mqtt_config() -> Dict[str, Any]:
    """Get MQTT configuration at runtime.

    Uses get_mqtt_broker_url() which checks:
    1. Config-service (jarvis-mqtt-broker) — respects JARVIS_CONFIG_URL_STYLE
       so Docker containers get host.docker.internal automatically
    2. config.json fallback
    3. Default: localhost:1884
    """
    from utils.service_discovery import get_mqtt_broker_url

    node_id: str = Config.get_str("node_id", "unknown") or "unknown"

    broker_url = get_mqtt_broker_url()
    broker = "localhost"
    port = 1884
    if broker_url:
        url = broker_url
        if url.startswith("mqtt://"):
            url = url[7:]
        if ":" in url:
            broker, port_str = url.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                pass
        else:
            broker = url

    return {
        "topic": Config.get_str("mqtt_topic", f"jarvis/nodes/{node_id}/#") or f"jarvis/nodes/{node_id}/#",
        "broker": broker,
        "port": port,
        "username": Config.get_str("mqtt_username", "") or "",
        "password": Config.get_str("mqtt_password", "") or ""
    }


def handle_tts(details: Dict[str, Any]) -> None:
    message: str = details.get("message", "")
    try:
        tts_provider = get_tts_provider()
        tts_provider.speak(True, message)
    except (ValueError, Exception) as e:
        logger.debug("TTS skipped (no audio output)", message=message[:80], error=str(e))


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


def handle_action(details: Dict[str, Any]) -> None:
    """Verify and dispatch an interactive action (e.g. Send/Cancel button tap) to a command."""
    print(f"[ACTION] handle_action called: {details.get('command_name')} action={details.get('action_name')}", flush=True)
    logger.info("handle_action called", details=details)
    request_id: Optional[str] = details.get("request_id")
    if not request_id:
        logger.warning("action: missing request_id, ignoring")
        return

    # Actions from the device control endpoint are already JWT-authenticated
    # at the CC level. Skip verify to avoid multiprocess request_id mismatch.
    if not details.get("trusted") and not _verify_command(request_id):
        logger.warning("action: verification failed", request_id=request_id[:8])
        return

    command_name: str = details.get("command_name", "")
    action_name: str = details.get("action_name", "")
    context: Dict[str, Any] = details.get("context", {})

    if not command_name or not action_name:
        logger.warning("action: missing command_name or action_name", details=details)
        return

    from utils.command_discovery_service import get_command_discovery_service

    service = get_command_discovery_service()
    commands = service.get_all_commands()
    cmd = commands.get(command_name)

    if not cmd and command_name == "control_device":
        # Device control without HA bundle — dispatch directly to device protocol
        reply_id: str = details.get("reply_request_id") or request_id
        _handle_device_protocol_control(action_name, context, reply_id)
        return

    if not cmd:
        logger.warning("action: unknown command", command_name=command_name)
        reply_id_missing: str = details.get("reply_request_id") or request_id
        _post_action_result(reply_id_missing, False, f"Command '{command_name}' not found on this node")
        return

    user_id: Optional[int] = details.get("user_id")

    from jarvis_command_sdk import set_current_user_id
    set_current_user_id(user_id)
    try:
        response = cmd.handle_action(action_name, context)
        message = ""
        error_msg: Optional[str] = None
        if response.context_data:
            message = response.context_data.get("message", "")
            error_msg = response.context_data.get("error")
        if not response.success and response.error_details:
            error_msg = response.error_details

        logger.info(
            "Action handled",
            command=command_name,
            action=action_name,
            success=response.success,
            error=error_msg,
        )

        # Send result back to CC FIRST so mobile gets unblocked immediately
        reply_id: str = details.get("reply_request_id") or request_id
        input_req: Optional[Dict[str, Any]] = None
        if response.context_data and response.context_data.get("input_required"):
            input_req = response.context_data["input_required"]
        _post_action_result(reply_id, response.success, error_msg, input_required=input_req)

        # TTS for voice-originated actions (best-effort, never overwrite result)
        if message and not details.get("reply_request_id"):
            try:
                tts_provider = get_tts_provider()
                tts_provider.speak(True, message)
            except Exception as tts_err:
                logger.warning("TTS playback failed for action", error=str(tts_err))
    except Exception as e:
        logger.error("Action handler error", command=command_name, action=action_name, error=str(e))
        reply_id_err: str = details.get("reply_request_id") or request_id
        _post_action_result(reply_id_err, False, str(e))
    finally:
        set_current_user_id(None)


def _post_action_result(
    request_id: str, success: bool, error: Optional[str] = None,
    input_required: Optional[Dict[str, Any]] = None,
) -> None:
    """POST action result back to CC via HTTP for synchronous callers."""
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.warning("Cannot post action result: CC URL not resolved")
        return

    url = f"{base_url.rstrip('/')}/api/v0/device-control-results/{request_id}"
    payload: Dict[str, Any] = {"success": success, "error": error}
    if input_required:
        payload["input_required"] = input_required
    try:
        RestClient.post(url, data=payload, timeout=5)
        logger.debug("Posted action result to CC", request_id=request_id[:8], success=success)
    except Exception as e:
        logger.warning("Failed to post action result", error=str(e))


def handle_report_tools(details: Dict[str, Any]) -> None:
    """Report this node's tool definitions back to CC.

    CC sends this when the mobile app needs the node's tools for chat.
    Builds client_tools + available_commands from the command discovery
    service and POSTs them to the CC callback endpoint.
    """
    request_id: Optional[str] = details.get("reply_request_id")
    if not request_id:
        logger.warning("report_tools: missing reply_request_id, ignoring")
        return

    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url
    from utils.command_discovery_service import get_command_discovery_service

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.warning("Cannot report tools: CC URL not resolved")
        return

    try:
        print(f"[MQTT] report_tools starting, request={request_id[:8]}", flush=True)
        service = get_command_discovery_service()
        service.refresh_now()
        commands = service.get_all_commands()
        print(f"[MQTT] report_tools discovered {len(commands)} commands", flush=True)

        # Build date context for schema generation
        from clients.jarvis_command_center_client import JarvisCommandCenterClient
        cc_client = JarvisCommandCenterClient(base_url)
        date_context = cc_client.get_date_context()

        client_tools: List[Dict[str, Any]] = []
        available_commands: List[Dict[str, Any]] = []
        for cmd in commands.values():
            try:
                client_tools.append(cmd.to_openai_tool_schema(date_context))
                available_commands.append(cmd.get_command_schema(date_context))
            except Exception as e:
                print(f"[MQTT] Skipping {cmd.command_name}: {e}", flush=True)

        url = f"{base_url.rstrip('/')}/api/v0/mobile/node-tool-reports/{request_id}"
        print(f"[MQTT] report_tools posting {len(client_tools)} tools to {url[:60]}", flush=True)
        RestClient.post(
            url,
            data={
                "client_tools": client_tools,
                "available_commands": available_commands,
            },
            timeout=10,
        )
        print(f"[MQTT] report_tools done", flush=True)
        logger.info("Reported tools to CC", count=len(client_tools), request_id=request_id[:8])
    except Exception as e:
        print(f"[MQTT] report_tools FAILED: {e}", flush=True)
        logger.error("Failed to report tools", error=str(e))


def handle_enroll_voice(details: Dict[str, Any]) -> None:
    """Capture a voice sample via the node's mic and POST to CC for enrollment.

    The phone-mic enrollment path produces embeddings tied to the phone's
    acoustics — recognition on the node's mic then scores poorly. This
    handler closes that gap: same mic at enrollment time as at runtime.

    Expected ``details``::

        {
          "user_id": int,
          "household_id": str,
          "request_id": str,             # for result reply
          "prompt_text": str,            # echoed to mobile UI
          "duration_secs": float,        # default 8
          "preroll_secs": float,         # default 1.5 (TTS cue + reaction)
        }

    On completion the handler POSTs to CC's mobile callback with the
    verify score so the mobile UI can show success/score-too-low.
    """
    import os
    import time as _time
    import uuid as _uuid

    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    user_id = details.get("user_id")
    household_id = details.get("household_id")
    request_id = details.get("request_id")
    prompt_text = details.get("prompt_text") or ""
    duration_secs: float = float(details.get("duration_secs") or 8.0)
    preroll_secs: float = float(details.get("preroll_secs") or 1.5)

    if user_id is None or not household_id or not request_id:
        logger.warning(
            "enroll_voice: missing required fields",
            user_id=user_id,
            household_id=bool(household_id),
            request_id=bool(request_id),
        )
        return

    base_url = get_command_center_url() or ""
    if not base_url:
        logger.warning("enroll_voice: CC URL not resolved")
        return

    # Lazy import to avoid circular import at module load
    from scripts.voice_listener import get_audio_bus, wake_paused
    from scripts.speech_to_text import record_fixed_duration
    from core.helpers import get_tts_provider

    bus = get_audio_bus()
    if bus is None:
        logger.error("enroll_voice: no AudioBus available — voice_listener not running")
        _post_enrollment_result(base_url, request_id, success=False,
                                error="audio_bus_unavailable")
        return

    # Pause wake detection for the whole TTS-cue + record + upload window.
    # Reading the enrollment prompt near the node mic would otherwise
    # produce wake-like utterances that fire the wake loop and start a
    # competing voice-command flow concurrent with the enrollment record.
    with wake_paused():
        # 1) Audible cue via TTS so the user knows when to start reading.
        try:
            tts = get_tts_provider()
            tts.speak(False, "Read the prompt on your screen, starting now.")
        except Exception as e:
            logger.warning("enroll_voice: TTS cue failed (continuing)", error=str(e))

        # 2) Brief preroll so the user has time to start speaking.
        if preroll_secs > 0:
            _time.sleep(preroll_secs)

        # 3) Capture a fixed-length sample using the shared bus (no PyAudio
        #    contention with the wake detector).
        output_path = f"/tmp/enroll-{_uuid.uuid4().hex[:8]}.wav"
        try:
            recording = record_fixed_duration(
                bus, seconds=duration_secs, output_path=output_path,
                subscriber_name=f"enroll-{request_id[:8]}",
            )
        except Exception as e:
            logger.error("enroll_voice: record failed", error=str(e))
            _post_enrollment_result(base_url, request_id, success=False,
                                    error=f"record_failed: {e}")
            return

    # 4) POST the WAV to CC's whisper proxy for enrollment.
    enroll_url = (
        f"{base_url.rstrip('/')}/api/v0/media/whisper/voice-profiles/enroll"
        f"?user_id={user_id}"
    )
    try:
        import requests
        # RestClient doesn't ship a multipart helper, so build the request
        # by hand but reuse its auth-header builder so node X-API-Key auth
        # matches the rest of the node's traffic.
        headers = RestClient._build_auth_header()
        with open(recording.audio_file, "rb") as f:
            r = requests.post(
                enroll_url,
                headers=headers,
                files={"file": (
                    os.path.basename(recording.audio_file),
                    f,
                    "audio/wav",
                )},
                timeout=20,
            )
        r.raise_for_status()
        upload_resp = r.json() if r.content else {}

        logger.info("enroll_voice: enrollment uploaded",
                    user_id=user_id, request_id=request_id[:8],
                    response=upload_resp)
        _post_enrollment_result(
            base_url, request_id,
            success=True,
            user_id=user_id,
            response=upload_resp,
            duration_secs=recording.duration,
        )
    except Exception as e:
        logger.error("enroll_voice: upload failed", error=str(e))
        _post_enrollment_result(base_url, request_id, success=False,
                                error=f"upload_failed: {e}")
    finally:
        try:
            os.unlink(recording.audio_file)
        except OSError:
            pass


def _post_enrollment_result(
    base_url: str,
    request_id: str,
    *,
    success: bool,
    **extra: Any,
) -> None:
    """POST the enrollment outcome to CC's mobile-callback endpoint.

    Mirrors the report_tools pattern (file-based polling on the CC side).
    Mobile app polls the matching GET endpoint to surface the result.
    """
    from clients.rest_client import RestClient
    url = f"{base_url.rstrip('/')}/api/v0/mobile/voice-profile-results/{request_id}"
    try:
        RestClient.post(
            url,
            data={"success": success, **extra},
            timeout=10,
        )
    except Exception as e:
        logger.warning("enroll_voice: failed to POST result to CC", error=str(e))


def handle_tool_call(details: Dict[str, Any]) -> None:
    """Execute a tool call from CC's mobile chat and POST the result back.

    CC routes LLM-generated tool calls to the node for execution when the
    mobile app is chatting. The node runs the command locally and returns
    the result so CC can continue the conversation.
    """
    reply_request_id: Optional[str] = details.get("reply_request_id")
    tool_call_id: str = details.get("tool_call_id", "")
    command_name: str = details.get("command_name", "")
    arguments: Dict[str, Any] = details.get("arguments", {})
    print(f"[MQTT] tool_call received: {command_name} args={list(arguments.keys()) if isinstance(arguments, dict) else 'str'} user_id={details.get('user_id')}", flush=True)

    if not reply_request_id:
        logger.warning("tool_call: missing reply_request_id, ignoring")
        return

    if not command_name:
        logger.warning("tool_call: missing command_name, ignoring")
        _post_tool_call_result(reply_request_id, {
            "output": {"error": "Missing command_name"},
        })
        return

    from utils.command_discovery_service import get_command_discovery_service

    service = get_command_discovery_service()
    commands = service.get_all_commands()
    cmd = commands.get(command_name)

    if not cmd:
        # Command may have been installed after startup — refresh and retry
        service.refresh_now()
        commands = service.get_all_commands()
        cmd = commands.get(command_name)

    if not cmd:
        logger.warning("tool_call: unknown command", command_name=command_name)
        _post_tool_call_result(reply_request_id, {
            "output": {"error": f"Unknown command: {command_name}"},
        })
        return

    try:
        from jarvis_command_sdk import RequestInformation
        from jarvis_command_sdk.context import set_current_user_id

        # Parse arguments if they're a JSON string
        if isinstance(arguments, str):
            import json as _json
            try:
                arguments = _json.loads(arguments)
            except (ValueError, TypeError):
                arguments = {}

        user_id: int | None = details.get("user_id")

        ri = RequestInformation(
            voice_command="[mobile chat]",
            conversation_id=tool_call_id or "mobile",
            user_id=user_id,
        )

        logger.info("Executing tool call", command=command_name, args=list(arguments.keys()), user_id=user_id)
        set_current_user_id(user_id)
        try:
            response = cmd.run(ri, **arguments)
        finally:
            set_current_user_id(None)

        # Build output from CommandResponse
        output: Dict[str, Any] = {}
        if response.context_data:
            output = response.context_data
        if not response.success:
            output["error"] = response.error_details or "Command failed"
        output["success"] = response.success

        # Include actions (e.g., Send/Cancel buttons) if present
        if response.actions:
            output["actions"] = [a.to_dict() for a in response.actions]

        logger.info("Tool call completed", command=command_name, success=response.success,
                     has_actions=bool(response.actions))
        _post_tool_call_result(reply_request_id, {"output": output})

    except Exception as e:
        logger.error("Tool call execution failed", command=command_name, error=str(e))
        _post_tool_call_result(reply_request_id, {
            "output": {"error": str(e), "success": False},
        })


def _post_tool_call_result(request_id: str, result: Dict[str, Any]) -> None:
    """POST tool call result back to CC for the mobile chat polling loop."""
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.warning("Cannot post tool call result: CC URL not resolved")
        return

    # Reuse the same result endpoint as device control
    url = f"{base_url.rstrip('/')}/api/v0/device-control-results/{request_id}"
    print(f"[MQTT] posting tool_call result to {url[:60]}.../{request_id[:8]}", flush=True)
    try:
        resp = RestClient.post(url, data=result, timeout=5)
        print(f"[MQTT] tool_call result posted: {resp}", flush=True)
    except Exception as e:
        print(f"[MQTT] tool_call result POST failed: {e}", flush=True)


def handle_toggle_command(details: Dict[str, Any]) -> None:
    """Enable or disable a command in the local command_registry.

    Published by CC when the user toggles smart_home.use_external_devices.
    Prevents duplicate control_device commands (built-in vs Pantry package).
    """
    command_name: str = details.get("command_name", "")
    enabled: bool = str(details.get("enabled", "true")).lower() in ("true", "1", "yes")

    if not command_name:
        logger.warning("toggle_command: missing command_name, ignoring")
        return

    try:
        from db import SessionLocal
        from repositories.command_registry_repository import CommandRegistryRepository

        db = SessionLocal()
        try:
            repo = CommandRegistryRepository(db)
            repo.set_enabled(command_name, enabled)
        finally:
            db.close()

        # Refresh discovery so next warmup picks up the change
        from utils.command_discovery_service import get_command_discovery_service
        get_command_discovery_service().refresh_now()

        print(f"[MQTT] toggle_command: {command_name} enabled={enabled}", flush=True)
        logger.info("Command toggled", command_name=command_name, enabled=enabled)

    except Exception as e:
        logger.error("toggle_command failed", command_name=command_name, error=str(e))


def handle_update_node_config(details: Dict[str, Any]) -> None:
    """Update node config.json values from mobile app.

    Writes key/value pairs to config.json. Changes to most settings
    take effect immediately (Config re-reads on each access). Settings
    captured at module level (wake_word_threshold, barge_in_threshold)
    require a service restart — the caller can request one via the
    ``restart`` flag.

    Expected details:
        settings: dict of key/value pairs to merge into config.json
        restart: bool (optional) — restart the service after applying
    """
    settings: Dict[str, Any] = details.get("settings", {})
    if not settings:
        logger.warning("update_node_config: no settings provided")
        return

    try:
        config_path = os.path.expandvars(os.path.expanduser(
            os.environ.get("CONFIG_PATH", "config.json")
        ))

        # Read current config
        try:
            with open(config_path) as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            config = {}

        # Merge new settings
        config.update(settings)

        # Write back
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

        logger.info("Node config updated via MQTT", keys=list(settings.keys()))
        print(f"[MQTT] update_node_config: updated {list(settings.keys())}", flush=True)

        # Restart if requested (for module-level settings like wake_word_threshold)
        if details.get("restart"):
            logger.info("Restarting service after config update")
            import subprocess
            subprocess.Popen(
                ["sudo", "systemctl", "restart", "jarvis-node"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    except Exception as e:
        logger.error("update_node_config failed", error=str(e))


def handle_invalidate_device_cache(details: Dict[str, Any]) -> None:
    """Drop the DirectDeviceService cache so the next list/get refreshes from CC.

    CC publishes this to every node in the household whenever a device is
    added, updated, or deleted. Without it, ``control_device`` could miss
    a freshly-added device for up to 5 minutes (until the periodic
    DeviceDiscoveryAgent refresh) and report "device not found" errors
    from a stale cache.
    """
    try:
        from services.direct_device_service import get_direct_device_service
        get_direct_device_service().invalidate_cache()
        logger.info("Device cache invalidated by CC notification")
    except Exception as e:
        logger.warning("invalidate_device_cache failed", error=str(e))


command_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "tts": handle_tts,
    "train_adapter": handle_train_adapter,
    "action": handle_action,
    "report_tools": handle_report_tools,
    "tool_call": handle_tool_call,
    "toggle_command": handle_toggle_command,
    "update_node_config": handle_update_node_config,
    "enroll_voice": handle_enroll_voice,
    "invalidate_device_cache": handle_invalidate_device_cache,
}


def on_connect(client: mqtt.Client, userdata: Any, flags: Dict[str, int], rc: int) -> None:
    logger.info("MQTT connected", result_code=rc)
    topic = get_mqtt_config()["topic"]
    client.subscribe(topic, qos=1)
    logger.info("MQTT subscribed", topic=topic)

    # Subscribe to auth-ready notifications from JCC OAuth flow
    client.subscribe("jarvis/auth/+/ready", qos=1)
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

    # No command matched — check device families
    from utils.device_family_discovery_service import get_device_family_discovery_service

    family_service = get_device_family_discovery_service()
    families = family_service.get_all_families_for_snapshot()

    for family in families.values():
        if family.authentication and family.authentication.provider == provider:
            logger.info("Storing auth credentials", provider=provider, family=family.protocol_name)
            family.store_auth_values(result)
            # Refresh discovery cache so next scan includes this family
            family_service.refresh()
            return

    logger.warning("No command or device family found for auth provider", provider=provider)


def _handle_k2_provision(raw_payload: bytes) -> None:
    """Handle K2 encryption key provisioned via MQTT (for Docker/headless nodes)."""
    try:
        data: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in K2 provision message")
        return

    k2 = data.get("k2", "")
    kid = data.get("kid", "")
    created_at = data.get("created_at", "")

    if not k2 or not kid:
        logger.warning("K2 provision message missing required fields")
        return

    request_id: str = data.get("request_id", "")

    try:
        from datetime import datetime
        from utils.encryption_utils import save_k2

        dt = datetime.fromisoformat(created_at) if created_at else datetime.utcnow()
        save_k2(k2, kid, dt)
        logger.info("K2 encryption key provisioned via MQTT", kid=kid)
        print(f"[MQTT] K2 provisioned: kid={kid}", flush=True)

        # Acknowledge receipt to CC
        if request_id:
            _ack_k2_provision(request_id, success=True)
    except Exception as e:
        logger.error("K2 provisioning failed", error=str(e))
        print(f"[MQTT] K2 provision error: {e}", flush=True)
        if request_id:
            _ack_k2_provision(request_id, success=False, error=str(e))


# Persistent event loop for device protocol calls. Protocols like Apple TV
# store pairing state (pyatv objects) that are bound to the event loop they
# were created on. Creating a new loop per call killed the pairing connection
# between pair_start and pair_finish.
_device_protocol_loop: Optional[asyncio.AbstractEventLoop] = None


def _get_device_protocol_loop() -> asyncio.AbstractEventLoop:
    """Return a persistent event loop for device protocol async calls."""
    global _device_protocol_loop
    if _device_protocol_loop is None or _device_protocol_loop.is_closed():
        _device_protocol_loop = asyncio.new_event_loop()
    return _device_protocol_loop


def _handle_device_protocol_control(action_name: str, context: Dict[str, Any], reply_id: str) -> None:
    """Dispatch device control directly to a device protocol (no command needed).

    Used when the control_device command isn't installed (e.g. standalone
    Govee/Nest protocols without the HA integration bundle).
    """
    protocol_name: str = context.get("protocol", "")
    entity_id: str = context.get("entity_id", "")

    if not protocol_name:
        _post_action_result(reply_id, False, "No protocol specified in device context")
        return

    try:
        from utils.device_family_discovery_service import get_device_family_discovery_service
        from jarvis_command_sdk import DiscoveredDevice

        svc = get_device_family_discovery_service()
        families = svc.get_all_families()

        protocol = families.get(protocol_name)
        if not protocol:
            # Try snapshot families (includes unconfigured ones)
            all_families = svc.get_all_families_for_snapshot()
            protocol = all_families.get(protocol_name)

        if not protocol:
            _post_action_result(reply_id, False, f"Device protocol '{protocol_name}' not found")
            return

        # Build a minimal DiscoveredDevice from the context
        device = DiscoveredDevice(
            entity_id=entity_id,
            name=context.get("name", entity_id),
            domain=context.get("domain", "switch"),
            manufacturer=protocol_name,
            model=context.get("model", ""),
            protocol=protocol_name,
            cloud_id=context.get("cloud_id"),
            local_ip=context.get("local_ip"),
            mac_address=context.get("mac_address"),
        )

        # Use persistent loop so protocol objects (e.g. pyatv pairing sessions)
        # survive across sequential calls like pair_start → pair_finish.
        loop = _get_device_protocol_loop()
        result = loop.run_until_complete(protocol.control(device, action_name, context))
        print(f"[ACTION] device protocol control: {protocol_name} {action_name} success={result.success}", flush=True)
        input_req = result.input_required.to_dict() if result.input_required else None
        _post_action_result(reply_id, result.success, result.error if not result.success else None, input_required=input_req)

    except Exception as e:
        print(f"[ACTION] device protocol control error: {e}", flush=True)
        import traceback
        traceback.print_exc()
        _post_action_result(reply_id, False, str(e))


def _ack_k2_provision(request_id: str, success: bool = True, error: str | None = None) -> None:
    """POST K2 provision acknowledgment back to CC."""
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    cc_url: str = get_command_center_url() or ""
    if not cc_url:
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""
    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/k2/ack/{request_id}"

    payload: Dict[str, Any] = {"success": success}
    if error:
        payload["error"] = error

    try:
        result = RestClient.post(url, data=payload, timeout=10)
        print(f"[MQTT] K2 ack posted: success={success}", flush=True)
    except Exception as e:
        print(f"[MQTT] K2 ack failed: {e}", flush=True)


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

    include_values: bool = notification.get("include_values", False)
    user_id: int | None = notification.get("user_id")
    logger.info("Settings snapshot requested", request_id=request_id[:8], include_values=include_values, user_id=user_id)
    thread = threading.Thread(target=_process_settings_request, args=(request_id, include_values, user_id), daemon=True)
    thread.start()


def _process_settings_request(request_id: str, include_values: bool = False, user_id: int | None = None) -> None:
    """Process a settings snapshot request (runs in background thread)."""
    try:
        print(f"[MQTT] processing settings request {request_id[:8]}", flush=True)
        # Debug: log snapshot command count
        from services.settings_snapshot_service import build_snapshot as _dbg_build
        _dbg_snapshot = _dbg_build(include_values=include_values, user_id=user_id)
        _dbg_cmds = [c["command_name"] for c in _dbg_snapshot.get("commands", [])]
        print(f"[MQTT] snapshot has {len(_dbg_cmds)} commands: {_dbg_cmds}", flush=True)
        success: bool = handle_snapshot_request(request_id, include_values=include_values, user_id=user_id)
        print(f"[MQTT] settings snapshot result: {success}", flush=True)
        if success:
            logger.info("Settings snapshot complete", request_id=request_id[:8])
        else:
            logger.error("Settings snapshot failed", request_id=request_id[:8])
    except Exception as e:
        print(f"[MQTT] settings snapshot error: {e}", flush=True)
        logger.error("Settings snapshot error", request_id=request_id[:8], error=str(e))


def _handle_device_list_notification(raw_payload: bytes) -> None:
    """Handle device list request from CC — runs collection in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in device list notification")
        return

    request_id: str = notification.get("request_id", "")
    manager_name: str = notification.get("manager_name", "jarvis_direct")
    if not request_id:
        logger.warning("Device list notification missing request_id")
        return

    logger.info("Device list requested", request_id=request_id[:8], manager=manager_name)

    from services.device_list_handler import run_collect_and_upload

    thread = threading.Thread(
        target=run_collect_and_upload, args=(request_id, manager_name), daemon=True
    )
    thread.start()


def _handle_device_state_notification(raw_payload: bytes) -> None:
    """Handle device state request from CC — runs query in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in device state notification")
        return

    request_id: str = notification.get("request_id", "")
    if not request_id:
        logger.warning("Device state notification missing request_id")
        return

    logger.info("Device state requested", request_id=request_id[:8])

    from services.device_state_handler import run_state_query_and_upload

    thread = threading.Thread(
        target=run_state_query_and_upload, args=(request_id, notification), daemon=True
    )
    thread.start()


def _handle_package_install_notification(raw_payload: bytes) -> None:
    """Handle package install request from CC — runs install in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in package install notification")
        return

    request_id: str = notification.get("request_id", "")
    command_name: str = notification.get("command_name", "")
    github_repo_url: str = notification.get("github_repo_url", "")
    git_tag: str | None = notification.get("git_tag")

    if not request_id or not github_repo_url:
        print(f"[INSTALL] missing request_id or github_repo_url, ignoring", flush=True)
        return

    print(f"[INSTALL] received: {command_name} from {github_repo_url} tag={git_tag}", flush=True)

    from services.package_install_handler import run_install_and_upload

    thread = threading.Thread(
        target=run_install_and_upload,
        args=(request_id, command_name, github_repo_url, git_tag),
        daemon=True,
    )
    thread.start()
    print(f"[INSTALL] thread started", flush=True)


def _handle_package_uninstall_notification(raw_payload: bytes) -> None:
    """Handle package uninstall request from CC — runs uninstall in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in package uninstall notification")
        return

    request_id: str = notification.get("request_id", "")
    command_name: str = notification.get("command_name", "")

    if not request_id or not command_name:
        print("[UNINSTALL] missing request_id or command_name, ignoring", flush=True)
        return

    print(f"[UNINSTALL] received: {command_name}", flush=True)

    from services.package_install_handler import run_uninstall_and_upload

    thread = threading.Thread(
        target=run_uninstall_and_upload,
        args=(request_id, command_name),
        daemon=True,
    )
    thread.start()
    print("[UNINSTALL] thread started", flush=True)


def _handle_factory_reset(raw_payload: bytes) -> None:
    """Handle factory-reset request from CC after node deletion.

    Security: Before resetting, calls CC's verify-reset endpoint to confirm
    the request_id was actually issued by CC. This prevents spoofed MQTT
    messages from triggering a factory reset.
    """
    try:
        data: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in factory-reset message")
        return

    request_id: str = data.get("request_id", "")
    node_id_from_msg: str = data.get("node_id", "")
    my_node_id: str = Config.get_str("node_id", "") or ""

    if not request_id or not node_id_from_msg:
        logger.warning("Factory-reset message missing required fields, ignoring")
        return

    if node_id_from_msg != my_node_id:
        logger.warning(
            "Factory-reset node_id mismatch, ignoring",
            expected=my_node_id[:8],
            received=node_id_from_msg[:8],
        )
        return

    print(f"[FACTORY-RESET] received reset request, verifying with CC...", flush=True)

    # Verify with CC that this reset was legitimately issued
    from utils.service_discovery import get_command_center_url

    cc_url: str = get_command_center_url() or ""
    if not cc_url:
        logger.warning("Cannot verify factory-reset: CC URL not resolved")
        return

    import requests

    verify_url: str = f"{cc_url.rstrip('/')}/api/v0/nodes/verify-reset"
    try:
        resp = requests.post(
            verify_url,
            json={"node_id": my_node_id, "request_id": request_id},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.error("Factory-reset verification request failed", error=str(e))
        return

    if resp.status_code != 200:
        logger.warning(
            "Factory-reset verification REJECTED by CC — ignoring (possible spoof)",
            status=resp.status_code,
        )
        print("[FACTORY-RESET] REJECTED by CC — ignoring", flush=True)
        return

    # Verified — proceed with factory reset
    print("[FACTORY-RESET] VERIFIED by CC — resetting node...", flush=True)
    logger.info("Factory-reset verified by CC, resetting node")

    try:
        from provisioning.factory_reset import factory_reset
        result: dict = factory_reset()
        logger.info("Factory reset complete", cleared=result.get("cleared", []))
        print(f"[FACTORY-RESET] complete: {result}", flush=True)
    except Exception as e:
        logger.error("Factory reset failed", error=str(e))
        print(f"[FACTORY-RESET] error: {e}", flush=True)
        return

    # Restart into provisioning mode
    print("[FACTORY-RESET] restarting into provisioning mode...", flush=True)
    logger.info("Restarting node into provisioning mode after factory reset")

    if _shutdown_event:
        _shutdown_event.set()
    else:
        import sys
        sys.exit(0)


def _handle_test_install_notification(raw_payload: bytes) -> None:
    """Handle test install nudge from CC — verify and install in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in test install notification")
        return

    request_id: str = notification.get("request_id", "")
    if not request_id:
        logger.warning("Test install notification missing request_id")
        return

    logger.info("Test install requested", request_id=request_id[:8])

    from services.test_install_handler import run_test_install_and_upload

    thread = threading.Thread(
        target=run_test_install_and_upload,
        args=(request_id,),
        daemon=True,
    )
    thread.start()


def _handle_device_scan_notification(raw_payload: bytes) -> None:
    """Handle device scan request from CC — runs scan in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in device scan notification")
        return

    request_id: str = notification.get("request_id", "")
    if not request_id:
        logger.warning("Device scan notification missing request_id")
        return

    logger.info("Device scan requested", request_id=request_id[:8])

    from services.device_scan_handler import run_scan_and_upload

    thread = threading.Thread(target=run_scan_and_upload, args=(request_id,), daemon=True)
    thread.start()


def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    print(f"[MQTT] message on {msg.topic}", flush=True)
    # Route by topic — auth-ready notifications from JCC OAuth flow
    if msg.topic.startswith("jarvis/auth/") and msg.topic.endswith("/ready"):
        _handle_auth_ready(msg.payload)
        return

    # Route by topic suffix — config push notifications are plain objects, not arrays
    if msg.topic.endswith("/k2/provision"):
        _handle_k2_provision(msg.payload)
        return

    if msg.topic.endswith("/config/push"):
        _handle_config_push_notification(msg.payload)
        return

    if msg.topic.endswith("/settings/request"):
        _handle_settings_request_notification(msg.payload)
        return

    if msg.topic.endswith("/device-list"):
        _handle_device_list_notification(msg.payload)
        return

    if msg.topic.endswith("/device-scan"):
        _handle_device_scan_notification(msg.payload)
        return

    if msg.topic.endswith("/device-state"):
        _handle_device_state_notification(msg.payload)
        return

    if msg.topic.endswith("/test-install"):
        _handle_test_install_notification(msg.payload)
        return

    if msg.topic.endswith("/package-install"):
        _handle_package_install_notification(msg.payload)
        return

    if msg.topic.endswith("/package-uninstall"):
        _handle_package_uninstall_notification(msg.payload)
        return

    if msg.topic.endswith("/factory-reset"):
        _handle_factory_reset(msg.payload)
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
        logger.debug("MQTT non-command message on topic", topic=msg.topic)
        return

    print(f"[MQTT] commands received: count={len(payload)} types={[c.get('command') for c in payload]}", flush=True)
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


_HEARTBEAT_INTERVAL_SECONDS = 300  # 5 minutes


def _heartbeat_loop() -> None:
    """Periodically POST heartbeat to command center to update last_seen.

    The response may carry a `pending_update` block the CC wants us to apply;
    the handler in update_service picks that up (added in a later step — for
    now the response is ignored).
    """
    from clients.rest_client import RestClient
    from core.runtime_state import is_busy
    from core.version import version_info
    from services.update_service import maybe_apply_update
    from utils.service_discovery import get_command_center_url

    # Initial delay: let service discovery initialize
    if _shutdown_event is not None:
        _shutdown_event.wait(timeout=10)
        if _shutdown_event.is_set():
            return
    else:
        time.sleep(10)

    while not (_shutdown_event is not None and _shutdown_event.is_set()):
        try:
            base_url: str = get_command_center_url() or ""
            if base_url:
                url = f"{base_url.rstrip('/')}/api/v0/admin/nodes/heartbeat"

                data: Dict[str, Any] = {
                    "version_info": version_info().to_dict(),
                    "is_busy": is_busy(),
                }
                if _tracked_threads is not None:
                    thread_status: Dict[str, bool] = {}
                    for name, entry in _tracked_threads.items():
                        thread_obj = entry[0] if isinstance(entry, tuple) else entry
                        thread_status[name] = thread_obj.is_alive() if hasattr(thread_obj, "is_alive") else False
                    data["thread_status"] = thread_status

                response = RestClient.post(url, data=data, timeout=10)
                # CC may return a pending_update block when the mobile app has
                # queued an upgrade. Hand it off to update_service, which forks
                # a detached installer and lets systemd do the restart dance.
                if response and isinstance(response, dict):
                    pending = response.get("pending_update")
                    if pending:
                        maybe_apply_update(pending)
        except Exception:
            pass  # Heartbeat is best-effort, retries next interval
        if _shutdown_event is not None:
            _shutdown_event.wait(timeout=_HEARTBEAT_INTERVAL_SECONDS)
        else:
            time.sleep(_HEARTBEAT_INTERVAL_SECONDS)


_TEST_CLEANUP_INTERVAL_SECONDS = 1200  # 20 minutes


def _test_command_cleanup_loop() -> None:
    """Periodically clean up expired test commands."""
    if _shutdown_event is not None:
        _shutdown_event.wait(timeout=60)
        if _shutdown_event.is_set():
            return
    else:
        time.sleep(60)  # Initial delay to let services start

    while not (_shutdown_event is not None and _shutdown_event.is_set()):
        try:
            from services.test_install_cleanup import cleanup_expired_test_commands
            count = cleanup_expired_test_commands()
            if count:
                logger.info("Test command cleanup cycle complete", removed=count)
        except Exception:
            pass  # Best-effort, retries next cycle
        if _shutdown_event is not None:
            _shutdown_event.wait(timeout=_TEST_CLEANUP_INTERVAL_SECONDS)
        else:
            time.sleep(_TEST_CLEANUP_INTERVAL_SECONDS)


def start_mqtt_listener(ma_service: MusicAssistantService) -> None:
    global _mqtt_client

    # Start heartbeat thread before MQTT (runs even if broker is unreachable)
    heartbeat_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    heartbeat_thread.start()
    logger.info("Heartbeat thread started", interval_seconds=_HEARTBEAT_INTERVAL_SECONDS)

    # Start test command cleanup thread (removes expired test installs every 20 min)
    cleanup_thread = threading.Thread(target=_test_command_cleanup_loop, daemon=True)
    cleanup_thread.start()
    logger.info("Test command cleanup thread started")

    config = get_mqtt_config()
    client: mqtt.Client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION1)

    if config["username"] and config["password"]:
        client.username_pw_set(config["username"], config["password"])

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = _on_disconnect

    # Enable paho-mqtt's built-in reconnect with exponential backoff
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    logger.info("MQTT listener starting", broker=config["broker"], port=config["port"])

    # Retry initial connection with exponential backoff
    max_attempts: int = 5
    for attempt in range(1, max_attempts + 1):
        try:
            client.connect(config["broker"], config["port"], 60)
            break
        except (ConnectionRefusedError, OSError) as e:
            if attempt == max_attempts:
                logger.error(
                    "MQTT broker not reachable after all retries, continuing without MQTT",
                    broker=config["broker"],
                    attempts=max_attempts,
                    error=str(e),
                )
                return
            backoff: int = 2 ** attempt
            logger.warning(
                "MQTT connection attempt failed, retrying",
                broker=config["broker"],
                attempt=attempt,
                max_attempts=max_attempts,
                retry_in_seconds=backoff,
                error=str(e),
            )
            time.sleep(backoff)

    _mqtt_client = client
    client.loop_forever()
