import asyncio
import json
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

import paho.mqtt.client as mqtt

from jarvis_log_client import JarvisLogger

from utils.audio_volume import set_volume_percent
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

# Bounded thread pool for MQTT message handlers. Caps concurrent threads
# (and their ~128KB-1MB stacks on ARM) to avoid OOM on Pi Zero (512MB).
_task_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="mqtt-task")

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

    Supported schemes: mqtt (raw TCP), mqtts (TLS TCP), ws (WebSocket),
    wss (TLS WebSocket — used by external nodes via Cloudflare Tunnel).
    """
    from utils.service_discovery import get_mqtt_broker_url

    node_id: str = Config.get_str("node_id", "unknown") or "unknown"

    broker_url = get_mqtt_broker_url() or "mqtt://localhost:1884"
    parsed = urlparse(broker_url)
    scheme = parsed.scheme or "mqtt"
    broker = parsed.hostname or "localhost"
    default_port = {"mqtt": 1883, "mqtts": 8883, "ws": 80, "wss": 443}.get(scheme, 1883)
    port = parsed.port or default_port

    return {
        "topic": Config.get_str("mqtt_topic", f"jarvis/nodes/{node_id}/#") or f"jarvis/nodes/{node_id}/#",
        "scheme": scheme,
        "broker": broker,
        "port": port,
        "username": Config.get_str("mqtt_username", "") or "",
        "password": Config.get_str("mqtt_password", "") or ""
    }


def handle_tts(details: Dict[str, Any]) -> None:
    message: str = details.get("message", "")
    try:
        from services.led_service import get_led_service
        get_led_service().set_transient_pattern("speaking")
    except Exception:
        pass
    try:
        tts_provider = get_tts_provider()
        tts_provider.speak(True, message)
    except (ValueError, Exception) as e:
        logger.debug("TTS skipped (no audio output)", message=message[:80], error=str(e))
    finally:
        try:
            from services.led_service import get_led_service
            get_led_service().set_transient_pattern(None)
        except Exception:
            pass


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

    _task_executor.submit(_run_training)
    logger.info("Adapter training task submitted", request_id=request_id[:8])


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


def handle_verify_voice(details: Dict[str, Any]) -> None:
    """Capture a voice sample via the node's mic and POST to CC for verification.

    Mirrors handle_enroll_voice but hits the verify endpoint instead. This
    ensures the verification sample uses the same mic as enrollment and
    runtime recognition.

    Expected ``details``::

        {
          "user_id": int,
          "household_id": str,
          "request_id": str,
          "prompt_text": str,
          "duration_secs": float,   # default 5
          "preroll_secs": float,    # default 1.5
        }
    """
    import os
    import time as _time
    import uuid as _uuid

    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    user_id = details.get("user_id")
    household_id = details.get("household_id")
    request_id = details.get("request_id")
    duration_secs: float = float(details.get("duration_secs") or 5.0)
    preroll_secs: float = float(details.get("preroll_secs") or 1.5)

    if user_id is None or not household_id or not request_id:
        logger.warning("verify_voice: missing required fields")
        return

    base_url = get_command_center_url() or ""
    if not base_url:
        logger.warning("verify_voice: CC URL not resolved")
        return

    from scripts.voice_listener import get_audio_bus, wake_paused
    from scripts.speech_to_text import record_fixed_duration
    from core.helpers import get_tts_provider

    bus = get_audio_bus()
    if bus is None:
        logger.error("verify_voice: no AudioBus available")
        _post_enrollment_result(base_url, request_id, success=False,
                                error="audio_bus_unavailable")
        return

    with wake_paused():
        try:
            tts = get_tts_provider()
            tts.speak(False, "Read the prompt on your screen, starting now.")
        except Exception as e:
            logger.warning("verify_voice: TTS cue failed (continuing)", error=str(e))

        if preroll_secs > 0:
            _time.sleep(preroll_secs)

        output_path = f"/tmp/verify-{_uuid.uuid4().hex[:8]}.wav"
        try:
            recording = record_fixed_duration(
                bus, seconds=duration_secs, output_path=output_path,
                subscriber_name=f"verify-{request_id[:8]}",
            )
        except Exception as e:
            logger.error("verify_voice: record failed", error=str(e))
            _post_enrollment_result(base_url, request_id, success=False,
                                    error=f"record_failed: {e}")
            return

    # POST the WAV to CC's whisper proxy for verification
    verify_url = (
        f"{base_url.rstrip('/')}/api/v0/media/whisper/voice-profiles/verify"
        f"?user_id={user_id}&household_id={household_id}"
    )
    try:
        import requests
        headers = RestClient._build_auth_header()
        with open(recording.audio_file, "rb") as f:
            r = requests.post(
                verify_url,
                headers=headers,
                files={"file": (
                    os.path.basename(recording.audio_file),
                    f,
                    "audio/wav",
                )},
                timeout=20,
            )
        r.raise_for_status()
        verify_resp = r.json() if r.content else {}

        logger.info("verify_voice: verification complete",
                    user_id=user_id, request_id=request_id[:8],
                    response=verify_resp)
        _post_enrollment_result(
            base_url, request_id,
            success=True,
            matched=verify_resp.get("matched", False),
            confidence=verify_resp.get("confidence", 0.0),
        )
    except Exception as e:
        logger.error("verify_voice: upload failed", error=str(e))
        _post_enrollment_result(base_url, request_id, success=False,
                                error=f"verify_failed: {e}")
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


# Settings still baked into a long-lived runtime object that re-reading
# config.json can't refresh — for these the node must restart to pick
# up changes. Everything else (volume, wake_word_threshold, barge_in_*)
# is read fresh on demand and applies live.
_KEYS_REQUIRING_RESTART: set[str] = {"wake_word_model"}


def handle_update_node_config(details: Dict[str, Any]) -> None:
    """Update node config.json values from mobile app.

    Writes key/value pairs to config.json and applies them live where
    possible. Volume goes through audio_volume.set_volume_percent (ALSA
    + PulseAudio + any BT sinks) so a connected speaker tracks too.

    A service restart only happens when the diff actually touches a key
    in ``_KEYS_REQUIRING_RESTART`` AND the caller asked for it — we used
    to restart on any non-volume change, which was the source of the
    "settings need a restart" frustration.

    Expected details:
        settings: dict of key/value pairs to merge into config.json
        restart: bool (optional) — restart the service after applying
    """
    settings: Dict[str, Any] = details.get("settings", {})
    if not settings:
        logger.warning("update_node_config: no settings provided")
        return

    # Volume is an OS-level setting. Apply immediately AND persist to
    # config.json so it survives reboot (alsa-restore is unreliable —
    # node startup re-applies from config).
    if "volume_percent" in settings:
        set_volume_percent(int(settings["volume_percent"]))

    # Live-apply LED preferences so the user sees the effect without
    # waiting for the next config reload. The Config (config.json) write
    # happens below so the value persists across reboots.
    if "led_enabled" in settings or "led_brightness_percent" in settings:
        try:
            from services.led_service import get_led_service
            led = get_led_service()
            if "led_enabled" in settings:
                val = settings["led_enabled"]
                enabled = bool(val) if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
                if hasattr(led, "set_enabled"):
                    led.set_enabled(enabled)
            if "led_brightness_percent" in settings:
                pct = int(settings["led_brightness_percent"])
                if hasattr(led, "set_brightness_scale"):
                    led.set_brightness_scale(pct)
        except Exception as e:
            logger.warning("LED live-apply failed", error=str(e))

    # is_muted: mirror the mute switch into PA right away (PA's sink
    # mute is the source of truth for runtime).
    if "is_muted" in settings:
        try:
            from utils.audio_volume import set_muted
            val = settings["is_muted"]
            muted = bool(val) if isinstance(val, bool) else str(val).lower() in ("true", "1", "yes")
            set_muted(muted)
        except Exception as e:
            logger.warning("Mute live-apply failed", error=str(e))

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

        restart_keys = set(settings) & _KEYS_REQUIRING_RESTART
        if details.get("restart") and restart_keys:
            logger.info("Restarting service after config update", keys=list(restart_keys))
            import subprocess
            subprocess.Popen(
                ["sudo", "systemctl", "restart", "jarvis-node"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

    except Exception as e:
        logger.error("update_node_config failed", error=str(e))


def handle_preview_led_pattern(details: Dict[str, Any]) -> None:
    """Briefly show ``pattern`` on the LED chain, then auto-revert.

    Lets the mobile "Test LEDs" picker preview a pattern without
    permanently altering the stable state. Patterns not recognized by the
    active LED service render as ``off``. Expected details:

        pattern: str — one of off/normal/alert/wake_detected/listening/
                       thinking/speaking/error/not_for_me/muted/shutdown_warning
        duration_seconds: float (optional, default 3.0)
    """
    pattern = str(details.get("pattern", "")).strip()
    if not pattern:
        logger.warning("preview_led_pattern: no pattern provided")
        return
    duration = float(details.get("duration_seconds", 3.0))

    try:
        from services.led_service import get_led_service
        led = get_led_service()
        if hasattr(led, "preview_pattern"):
            led.preview_pattern(pattern, duration_seconds=duration)
            logger.info("LED preview", pattern=pattern, duration_seconds=duration)
        else:
            logger.warning("LED service has no preview_pattern method")
    except Exception as e:
        logger.error("preview_led_pattern failed", pattern=pattern, error=str(e))


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


def handle_measure_ambient_noise(details: Dict[str, Any]) -> None:
    """Capture ambient audio and post back a suggested silence_threshold.

    Lets the mobile "Set Automatically" button calibrate the silence
    threshold from real room conditions. Uses the existing AudioBus so
    there's no PyAudio contention with the wake detector; wake detection
    is paused for the measurement window so the user doesn't accidentally
    wake the node mid-calibration.

    Expected ``details``::

        request_id: str
        duration_seconds: float  (optional, default 3.0)
    """
    import queue as _queue

    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    request_id: Optional[str] = details.get("request_id")
    if not request_id:
        logger.warning("measure_ambient_noise: missing request_id, ignoring")
        return

    if not _verify_command(request_id):
        logger.warning("measure_ambient_noise: verification failed",
                       request_id=request_id[:8])
        return

    duration_seconds: float = float(details.get("duration_seconds") or 3.0)
    duration_seconds = max(1.0, min(10.0, duration_seconds))

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.error("measure_ambient_noise: CC URL not resolved")
        return

    node_id: str = Config.get_str("node_id", "") or ""
    result_url = (
        f"{base_url.rstrip('/')}/api/v0/nodes/{node_id}"
        f"/ambient-noise-measurements/{request_id}/result"
    )

    def _post_result(payload: Dict[str, Any]) -> None:
        try:
            RestClient.post(result_url, data=payload, timeout=10)
        except Exception as e:
            logger.error("measure_ambient_noise: result POST failed", error=str(e))

    from scripts.voice_listener import get_audio_bus, wake_paused
    from scripts.speech_to_text import calculate_rms

    bus = get_audio_bus()
    if bus is None:
        logger.error("measure_ambient_noise: no AudioBus available")
        _post_result({"success": False, "error": "audio_bus_unavailable"})
        return

    subscriber_name = f"ambient-noise-{request_id[:8]}"
    rms_values: List[float] = []
    chunks_seen: int = 0
    chunk_secs: float = bus.chunk_samples / bus.rate
    chunk_get_timeout: float = max(chunk_secs * 4, 0.5)
    deadline: float = time.monotonic() + duration_seconds

    try:
        with wake_paused():
            q = bus.subscribe(subscriber_name)
            try:
                while time.monotonic() < deadline:
                    remaining = max(0.05, deadline - time.monotonic())
                    try:
                        data = q.get(timeout=min(chunk_get_timeout, remaining))
                    except _queue.Empty:
                        continue
                    rms_values.append(calculate_rms(data))
                    chunks_seen += 1
            finally:
                bus.unsubscribe(subscriber_name)
    except Exception as e:
        logger.error("measure_ambient_noise: capture failed", error=str(e))
        _post_result({"success": False, "error": f"capture_failed: {e}"})
        return

    if not rms_values:
        logger.warning("measure_ambient_noise: no audio captured")
        _post_result({"success": False, "error": "no_audio_captured"})
        return

    import numpy as np

    rms_array = np.array(rms_values, dtype=np.float32)
    p50 = float(np.percentile(rms_array, 50))
    p75 = float(np.percentile(rms_array, 75))
    p95 = float(np.percentile(rms_array, 95))
    max_rms = float(np.max(rms_array))

    # Suggested silence_threshold = P75 × 1.3, rounded to nearest 100, clamped
    # to the slider range. P75 is a more stable "typical ambient" estimate
    # than P95 — it excludes rare peaks (a chair scrape, the user moving)
    # that would otherwise push the threshold above the speech band. 1.3× of
    # that gives ~30% headroom over the upper-quartile noise floor, which is
    # enough to reject ambient while staying below conversational speech
    # (typically 2-4× the ambient floor on the dev Pi).
    suggested = int(round(p75 * 1.3 / 100.0)) * 100
    suggested = max(1000, min(10000, suggested))

    logger.info(
        "Ambient noise measurement complete",
        request_id=request_id[:8],
        chunks=chunks_seen,
        p50=round(p50, 1),
        p75=round(p75, 1),
        p95=round(p95, 1),
        max=round(max_rms, 1),
        suggested=suggested,
    )
    _post_result({
        "success": True,
        "duration_seconds": duration_seconds,
        "chunks": chunks_seen,
        "p50_rms": p50,
        "p75_rms": p75,
        "p95_rms": p95,
        "max_rms": max_rms,
        "suggested_silence_threshold": suggested,
    })


command_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "tts": handle_tts,
    "train_adapter": handle_train_adapter,
    "action": handle_action,
    "report_tools": handle_report_tools,
    "tool_call": handle_tool_call,
    "toggle_command": handle_toggle_command,
    "update_node_config": handle_update_node_config,
    "preview_led_pattern": handle_preview_led_pattern,
    "enroll_voice": handle_enroll_voice,
    "verify_voice": handle_verify_voice,
    "invalidate_device_cache": handle_invalidate_device_cache,
    "measure_ambient_noise": handle_measure_ambient_noise,
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
    _task_executor.submit(_pull_auth_credentials, provider)


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

    _task_executor.submit(_process_config_push)


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
    _task_executor.submit(_process_settings_request, request_id, include_values, user_id)


def _process_settings_request(request_id: str, include_values: bool = False, user_id: int | None = None) -> None:
    """Process a settings snapshot request (runs in background thread).

    On the Pi Zero this is CPU-heavy enough to starve the wake loop while
    it runs (SQLCipher reads + per-command secret decrypt + K2 re-encrypt).
    A debug print used to build the snapshot a second time here for its
    command count — removed 2026-05-20, halved the per-request CPU spike.
    The proper fix is a keyed snapshot cache invalidated on writes; see
    project memory ``project_snapshot_cache.md``.
    """
    try:
        print(f"[MQTT] processing settings request {request_id[:8]}", flush=True)
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

    _task_executor.submit(run_collect_and_upload, request_id, manager_name)


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

    _task_executor.submit(run_state_query_and_upload, request_id, notification)


def _handle_camera_credentials_notification(raw_payload: bytes) -> None:
    """Handle camera credential request from CC — runs lookup in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in camera credentials notification")
        return

    request_id: str = notification.get("request_id", "")
    if not request_id:
        logger.warning("Camera credentials notification missing request_id")
        return

    logger.info("Camera credentials requested", request_id=request_id[:8])

    from services.camera_credentials_handler import run_credentials_lookup_and_upload

    _task_executor.submit(run_credentials_lookup_and_upload, request_id, notification)


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

    _task_executor.submit(run_install_and_upload, request_id, command_name, github_repo_url, git_tag)
    print("[INSTALL] task submitted", flush=True)


def _handle_package_uninstall_notification(raw_payload: bytes) -> None:
    """Handle package uninstall request from CC — runs uninstall in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in package uninstall notification")
        return

    request_id: str = notification.get("request_id", "")
    command_name: str = notification.get("command_name", "")
    component_type: str | None = notification.get("component_type")

    if not request_id or not command_name:
        print("[UNINSTALL] missing request_id or command_name, ignoring", flush=True)
        return

    print(f"[UNINSTALL] received: {command_name} (type={component_type})", flush=True)

    from services.package_install_handler import run_uninstall_and_upload

    _task_executor.submit(run_uninstall_and_upload, request_id, command_name, component_type)
    print("[UNINSTALL] task submitted", flush=True)


def _post_factory_reset_status(
    cc_url: str,
    task_id: str,
    token: str,
    state: str,
    error_message: str | None = None,
) -> bool:
    """PATCH the CC factory-reset task with current state.

    The token is sent in the X-Reset-Token header (the node's API key has
    been wiped by the time the final "success" call goes out). A valid
    token doubles as proof the request was legitimately issued by CC —
    this replaces the old standalone /verify-reset call.

    Returns True on a 2xx response, False otherwise. Failures here are
    logged but never abort the reset itself: better to wipe the device
    and let the mobile UI time out than leave a node in a half-state
    because of a transient network error.
    """
    import requests

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/factory-reset/{task_id}/status"
    body: Dict[str, Any] = {"state": state}
    if error_message is not None:
        body["error_message"] = error_message
    try:
        resp = requests.post(
            url,
            json=body,
            headers={"X-Reset-Token": token},
            timeout=10,
        )
    except requests.RequestException as e:
        logger.warning(
            "Factory-reset status post failed",
            state=state,
            error=str(e),
        )
        return False

    if resp.status_code // 100 != 2:
        logger.warning(
            "Factory-reset status post returned non-2xx",
            state=state,
            status=resp.status_code,
            body=resp.text[:200],
        )
        return False
    return True


def _reboot_after_factory_reset() -> None:
    """Trigger a full OS reboot.

    Falls back to a graceful exit (which systemd restarts) if reboot
    isn't available — that still gets the node into provisioning mode,
    just without clearing OS-level state.
    """
    try:
        # Detached so this function returns and we don't block the MQTT
        # callback while the system is going down.
        subprocess.Popen(
            ["sudo", "reboot"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    except FileNotFoundError as e:
        logger.warning("sudo reboot not available, falling back to graceful exit", error=str(e))

    if _shutdown_event:
        _shutdown_event.set()
    else:
        import sys
        sys.exit(0)


def _handle_factory_reset(raw_payload: bytes) -> None:
    """Handle factory-reset request from CC.

    Flow:
      1. Parse {request_id, node_id, task_id} from MQTT payload.
      2. PATCH state="in_progress" to CC (also serves as auth check —
         a 401 here means the token is invalid, so abort).
      3. Wipe node-local state via factory_reset().
      4. PATCH state="success" using the cached cc_url + token + task_id.
      5. Reboot.

    All state needed for the post-wipe PATCH is held in local variables
    (factory_reset() runs in-process and doesn't restart Python), so
    nothing needs to be persisted to disk before the wipe.
    """
    try:
        data: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in factory-reset message")
        return

    request_id: str = data.get("request_id", "")
    node_id_from_msg: str = data.get("node_id", "")
    task_id: str = data.get("task_id", "")
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

    from utils.service_discovery import get_command_center_url

    cc_url: str = get_command_center_url() or ""
    if not cc_url:
        logger.warning("Cannot run factory-reset: CC URL not resolved")
        return

    if not task_id:
        # Older CC versions still publish without task_id — fall back to
        # the legacy verify-reset path so an in-flight upgrade doesn't
        # leave a node un-resettable.
        logger.warning(
            "Factory-reset message has no task_id, using legacy verify-reset path"
        )
        import requests
        verify_url = f"{cc_url.rstrip('/')}/api/v0/nodes/verify-reset"
        try:
            resp = requests.post(
                verify_url,
                json={"node_id": my_node_id, "request_id": request_id},
                timeout=10,
            )
        except requests.RequestException as e:
            logger.error("Legacy verify-reset failed", error=str(e))
            return
        if resp.status_code != 200:
            logger.warning(
                "Legacy verify-reset REJECTED",
                status=resp.status_code,
            )
            return
    else:
        # New path: status PATCH validates the token AND surfaces progress.
        print("[FACTORY-RESET] reporting in_progress to CC...", flush=True)
        if not _post_factory_reset_status(cc_url, task_id, request_id, "in_progress"):
            logger.warning(
                "Factory-reset rejected at status endpoint — aborting (possible spoof or expired token)"
            )
            print("[FACTORY-RESET] REJECTED by CC — ignoring", flush=True)
            return

    print("[FACTORY-RESET] resetting node...", flush=True)
    logger.info("Running factory_reset()", task_id=task_id[:8] if task_id else "(legacy)")

    try:
        from provisioning.factory_reset import factory_reset
        result: dict = factory_reset()
        logger.info("Factory reset complete", cleared=result.get("cleared", []))
        print(f"[FACTORY-RESET] complete: {result}", flush=True)
    except Exception as e:
        logger.error("Factory reset failed", error=str(e))
        print(f"[FACTORY-RESET] error: {e}", flush=True)
        if task_id:
            _post_factory_reset_status(cc_url, task_id, request_id, "failed", error_message=str(e))
        return

    # Report success BEFORE rebooting — once reboot fires we lose the
    # opportunity. The token + task_id were captured before the wipe and
    # the wipe didn't touch them (cc_url field in config.json is also
    # preserved, but we've kept everything in local vars anyway).
    if task_id:
        _post_factory_reset_status(cc_url, task_id, request_id, "success")

    print("[FACTORY-RESET] rebooting...", flush=True)
    logger.info("Rebooting node after factory reset")
    _reboot_after_factory_reset()


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

    _task_executor.submit(run_test_install_and_upload, request_id)


def _handle_bluetooth_scan_notification(raw_payload: bytes) -> None:
    """Handle Bluetooth scan request from CC — runs scan in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in bluetooth scan notification")
        return

    request_id: str = notification.get("request_id", "")
    if not request_id:
        logger.warning("Bluetooth scan notification missing request_id")
        return

    role: str = notification.get("role", "source")
    logger.info("Bluetooth scan requested", request_id=request_id[:8], role=role)

    from services.bluetooth_scan_handler import run_bluetooth_scan_and_upload

    _task_executor.submit(run_bluetooth_scan_and_upload, request_id, role)


def _handle_bluetooth_pair_notification(raw_payload: bytes) -> None:
    """Handle Bluetooth pair request from CC — runs pair in background thread."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in bluetooth pair notification")
        return

    request_id: str = notification.get("request_id", "")
    mac_address: str = notification.get("mac_address", "")
    role: str = notification.get("role", "source")

    if not request_id or not mac_address:
        logger.warning("Bluetooth pair notification missing request_id or mac_address")
        return

    logger.info("Bluetooth pair requested", request_id=request_id[:8], mac=mac_address)

    from services.bluetooth_scan_handler import run_bluetooth_pair_and_upload

    _task_executor.submit(run_bluetooth_pair_and_upload, request_id, mac_address, role)


def _handle_bluetooth_disconnect_notification(raw_payload: bytes) -> None:
    """Handle Bluetooth disconnect request from CC — fire-and-forget."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in bluetooth disconnect notification")
        return

    mac_address: str = notification.get("mac_address", "")
    if not mac_address:
        logger.warning("Bluetooth disconnect notification missing mac_address")
        return

    logger.info("Bluetooth disconnect requested", mac=mac_address)

    from services.bluetooth_scan_handler import run_bluetooth_disconnect

    _task_executor.submit(run_bluetooth_disconnect, mac_address)


def _handle_bluetooth_discoverable_notification(raw_payload: bytes) -> None:
    """Handle Bluetooth discoverable request from CC — fire-and-forget."""
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in bluetooth discoverable notification")
        return

    timeout: int = notification.get("timeout", 120)
    logger.info("Bluetooth discoverable requested", timeout=timeout)

    from services.bluetooth_scan_handler import run_bluetooth_discoverable

    _task_executor.submit(run_bluetooth_discoverable, timeout)


def _handle_bluetooth_release_notification(raw_payload: bytes) -> None:
    """Handle Bluetooth release ("forget" or "release for phone") from CC.

    Payload: {mac_address: str, forget?: bool}
      - forget=False (default): disconnect + auto_connect=False (keeps pair).
      - forget=True: disconnect + remove + delete storage record.
    """
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in bluetooth release notification")
        return

    mac_address: str = notification.get("mac_address", "")
    if not mac_address:
        logger.warning("Bluetooth release notification missing mac_address")
        return

    forget: bool = bool(notification.get("forget", False))
    logger.info("Bluetooth release requested", mac=mac_address, forget=forget)

    from services.bluetooth_scan_handler import run_bluetooth_release

    _task_executor.submit(run_bluetooth_release, mac_address, forget)


def _handle_bluetooth_auto_connect_notification(raw_payload: bytes) -> None:
    """Handle auto-connect toggle from CC.

    Payload: {mac_address: str, enabled: bool}
    """
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in bluetooth auto-connect notification")
        return

    mac_address: str = notification.get("mac_address", "")
    if not mac_address:
        logger.warning("Bluetooth auto-connect notification missing mac_address")
        return

    enabled: bool = bool(notification.get("enabled", True))
    logger.info("Bluetooth auto-connect toggle", mac=mac_address, enabled=enabled)

    from services.bluetooth_scan_handler import run_bluetooth_set_auto_connect

    _task_executor.submit(run_bluetooth_set_auto_connect, mac_address, enabled)


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

    _task_executor.submit(run_scan_and_upload, request_id)


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

    if msg.topic.endswith("/bluetooth-scan"):
        _handle_bluetooth_scan_notification(msg.payload)
        return

    if msg.topic.endswith("/bluetooth-pair"):
        _handle_bluetooth_pair_notification(msg.payload)
        return

    if msg.topic.endswith("/bluetooth-disconnect"):
        _handle_bluetooth_disconnect_notification(msg.payload)
        return

    if msg.topic.endswith("/bluetooth-discoverable"):
        _handle_bluetooth_discoverable_notification(msg.payload)
        return

    if msg.topic.endswith("/bluetooth-release"):
        _handle_bluetooth_release_notification(msg.payload)
        return

    if msg.topic.endswith("/bluetooth-auto-connect"):
        _handle_bluetooth_auto_connect_notification(msg.payload)
        return

    if msg.topic.endswith("/device-scan"):
        _handle_device_scan_notification(msg.payload)
        return

    if msg.topic.endswith("/device-state"):
        _handle_device_state_notification(msg.payload)
        return

    if msg.topic.endswith("/camera-credentials"):
        _handle_camera_credentials_notification(msg.payload)
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


def _get_memory_stats() -> Dict[str, Any]:
    """Collect lightweight memory stats for heartbeat reporting.

    Always includes RSS and thread count (near-zero overhead).
    When JARVIS_MEMORY_PROFILING=1, also logs tracemalloc top allocators.
    """
    import resource

    rusage = resource.getrusage(resource.RUSAGE_SELF)
    stats: Dict[str, Any] = {
        "rss_mb": round(rusage.ru_maxrss / (1024 * 1024), 1),  # maxrss is bytes on Linux, KB on macOS
        "thread_count": threading.active_count(),
        "pool_queue_size": _task_executor._work_queue.qsize(),
    }

    if os.environ.get("JARVIS_MEMORY_PROFILING") == "1":
        import tracemalloc
        if not tracemalloc.is_tracing():
            tracemalloc.start()
        snapshot = tracemalloc.take_snapshot()
        top = snapshot.statistics("lineno")[:10]
        stats["tracemalloc_top"] = [
            {"file": str(s.traceback), "size_kb": round(s.size / 1024, 1)}
            for s in top
        ]
        current, peak = tracemalloc.get_traced_memory()
        stats["traced_current_mb"] = round(current / (1024 * 1024), 1)
        stats["traced_peak_mb"] = round(peak / (1024 * 1024), 1)
        logger.info("Memory profile", **stats)

    return stats


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
    from utils.encryption_utils import has_k2
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

                from utils.device_family_discovery_service import get_device_family_discovery_service
                families = get_device_family_discovery_service().get_all_families()

                data: Dict[str, Any] = {
                    "version_info": version_info().to_dict(),
                    "is_busy": is_busy(),
                    "protocols": sorted(families.keys()),
                    "needs_k2": not has_k2(),
                    "memory": _get_memory_stats(),
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
    scheme: str = config["scheme"]
    transport: str = "websockets" if scheme in ("ws", "wss") else "tcp"

    client: mqtt.Client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1,
        transport=transport,
    )

    # TLS for wss (Cloudflare cert) and mqtts. Default system CA bundle.
    if scheme in ("wss", "mqtts"):
        client.tls_set()

    if config["username"] and config["password"]:
        client.username_pw_set(config["username"], config["password"])

    client.on_connect = on_connect
    client.on_message = on_message
    client.on_disconnect = _on_disconnect

    # Enable paho-mqtt's built-in reconnect with exponential backoff
    client.reconnect_delay_set(min_delay=1, max_delay=60)

    logger.info(
        "MQTT listener starting",
        scheme=scheme,
        broker=config["broker"],
        port=config["port"],
        transport=transport,
    )

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
