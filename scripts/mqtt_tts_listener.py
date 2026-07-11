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

from constants.config_policy import UPDATE_POLICY_KEYS
from utils.audio_volume import set_volume_percent
from utils.config_file import update_config_file
from utils.config_service import Config
from utils.mqtt_credentials import fetch_and_persist_mqtt_credentials
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

    Resolves the broker from config-service via get_mqtt_broker_url() — the same
    uniform resolver used for every service (respects JARVIS_CONFIG_URL_STYLE, so
    a Docker peer gets host.docker.internal and an off-box node gets the server
    IP). There is deliberately NO localhost fallback: get_mqtt_broker_url() raises
    ServiceUnresolvedError when config-service can't resolve the broker (unless the
    node is still provisioning), and the connect loop retries with backoff rather
    than connecting to a fabricated localhost that reaches nothing.

    Supported schemes: mqtt (raw TCP), mqtts (TLS TCP), ws (WebSocket),
    wss (TLS WebSocket — used by external nodes via Cloudflare Tunnel).
    """
    from utils.service_discovery import get_mqtt_broker_url, ServiceUnresolvedError

    node_id: str = Config.get_str("node_id", "unknown") or "unknown"

    broker_url = get_mqtt_broker_url()
    parsed = urlparse(broker_url)
    scheme = parsed.scheme or "mqtt"
    broker = parsed.hostname
    if not broker:
        raise ServiceUnresolvedError(
            f"Resolved MQTT broker URL has no host: {broker_url!r} — refusing to "
            "fall back to localhost."
        )
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


def handle_callback(details: Dict[str, Any]) -> None:
    """Dispatch an interactive-notification callback to a command's @callback method.

    Zero-trust delivery: MQTT carries only an opaque request_id (== job_id).
    The full {command_name, callback_name, data, user_id} payload is fetched
    from command-center over authenticated HTTPS (node X-API-Key). Result is
    posted back to CC, which produces the follow-up inbox item.

    Wire format matches every other node command: CC publishes via
    NodeCommandService.publish_command_with_id, which puts the id in
    ``details["request_id"]`` — same shape as the "action" command. We use
    the GET endpoint as our verification step (it authenticates the node
    and checks job ownership), so we don't call _verify_command here.
    """
    job_id: Optional[str] = details.get("request_id")
    if not job_id:
        logger.warning("callback: missing request_id, ignoring")
        return

    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.warning("Cannot fetch callback payload: CC URL not resolved", job_id=job_id[:8])
        return

    fetch_url = f"{base_url.rstrip('/')}/api/v0/callbacks/{job_id}"
    payload: Optional[Dict[str, Any]] = RestClient.get(fetch_url, timeout=10)
    if payload is None:
        logger.warning("Failed to fetch callback payload from CC", job_id=job_id[:8])
        return

    command_name: str = payload.get("command_name", "")
    callback_name: str = payload.get("callback_name", "")
    data: Dict[str, Any] = payload.get("data", {}) or {}
    user_id: Optional[int] = payload.get("user_id")
    conversation_id: str = payload.get("conversation_id") or f"callback:{job_id}"
    voice_command: str = payload.get("voice_command") or f"cb:{callback_name}"

    if not command_name or not callback_name:
        logger.warning("callback: missing command_name or callback_name", payload=payload)
        _post_callback_result(job_id, False, "missing command_name or callback_name")
        return

    from utils.command_discovery_service import get_command_discovery_service

    cmd = get_command_discovery_service().get_command(command_name)
    if cmd is None:
        logger.warning("callback: unknown command", command_name=command_name)
        _post_callback_result(job_id, False, f"Command '{command_name}' not found on this node")
        return

    # Older SDKs (pre-0.2.0) won't have get_callbacks — fail soft.
    get_callbacks = getattr(cmd, "get_callbacks", None)
    if not callable(get_callbacks):
        logger.warning("callback: command predates @callback SDK", command_name=command_name)
        _post_callback_result(job_id, False, f"Command '{command_name}' does not support callbacks")
        return

    try:
        callbacks = get_callbacks()
    except Exception as e:
        logger.error("callback: get_callbacks raised", command_name=command_name, error=str(e))
        _post_callback_result(job_id, False, f"get_callbacks failed: {e}")
        return

    callback_fn = callbacks.get(callback_name)
    if callback_fn is None:
        logger.warning(
            "callback: unknown callback name",
            command_name=command_name,
            callback_name=callback_name,
            available=list(callbacks.keys()),
        )
        _post_callback_result(
            job_id, False,
            f"Callback '{callback_name}' not registered on '{command_name}'",
        )
        return

    from jarvis_command_sdk import RequestInformation, set_current_user_id

    set_current_user_id(user_id)
    try:
        request_info = RequestInformation(
            voice_command=voice_command,
            conversation_id=conversation_id,
            user_id=user_id,
        )
        response = callback_fn(data, request_info)

        error_msg: Optional[str] = None
        if response.context_data:
            error_msg = response.context_data.get("error")
        if not response.success and response.error_details:
            error_msg = response.error_details

        logger.info(
            "Callback handled",
            command=command_name,
            callback=callback_name,
            success=response.success,
            error=error_msg,
        )
        _post_callback_result(
            job_id,
            response.success,
            error_msg,
            context_data=response.context_data,
        )
    except Exception as e:
        logger.error(
            "Callback handler error",
            command=command_name,
            callback=callback_name,
            error=str(e),
        )
        _post_callback_result(job_id, False, str(e))
    finally:
        set_current_user_id(None)


def _post_callback_result(
    job_id: str,
    success: bool,
    error: Optional[str] = None,
    context_data: Optional[Dict[str, Any]] = None,
) -> None:
    """POST callback execution result back to CC.

    CC turns this into the follow-up inbox item the mobile app sees.
    """
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.warning("Cannot post callback result: CC URL not resolved")
        return

    url = f"{base_url.rstrip('/')}/api/v0/callbacks/{job_id}/result"
    payload: Dict[str, Any] = {"success": success, "error": error}
    if context_data is not None:
        payload["context_data"] = context_data
    try:
        RestClient.post(url, data=payload, timeout=10)
        logger.debug("Posted callback result to CC", job_id=job_id[:8], success=success)
    except Exception as e:
        logger.warning("Failed to post callback result", error=str(e))


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

        # Installed package versions — lets mobile compare against Pantry's
        # latest_version and offer Install / Update / Up-to-date instead of
        # the binary "already installed" check it used to do. Each entry also
        # carries `previous_version` (drives the "Revert to X" action) and
        # `health` ("failed" when any of the package's components is in a
        # discovery failed-modules registry — red dot on the package card).
        installed_packages: List[Dict[str, Any]] = []
        try:
            from services.command_store_service import list_installed

            failed_modules: Dict[str, str] = {}
            try:
                failed_modules.update(service.get_failed_modules())
            except Exception:
                pass
            try:
                from utils.agent_discovery_service import get_agent_discovery_service
                failed_modules.update(
                    get_agent_discovery_service().get_failed_modules()
                )
            except Exception:
                pass

            for meta in list_installed():
                name = meta.get("package_name") or meta.get("command_name")
                version = meta.get("version")
                if name and version:
                    entry: Dict[str, Any] = {"name": name, "version": version}
                    if meta.get("previous_version"):
                        entry["previous_version"] = meta["previous_version"]
                    component_names = {
                        c.get("name") for c in meta.get("components", [])
                    }
                    entry["health"] = (
                        "failed" if component_names & set(failed_modules) else "ok"
                    )
                    installed_packages.append(entry)
        except Exception as e:
            print(f"[MQTT] installed_packages enumeration failed: {e}", flush=True)

        url = f"{base_url.rstrip('/')}/api/v0/mobile/node-tool-reports/{request_id}"
        print(f"[MQTT] report_tools posting {len(client_tools)} tools to {url[:60]}", flush=True)
        RestClient.post(
            url,
            data={
                "client_tools": client_tools,
                "available_commands": available_commands,
                "installed_packages": installed_packages,
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
        from utils.command_execution_service import _build_secrets

        # Parse arguments if they're a JSON string
        if isinstance(arguments, str):
            import json as _json
            try:
                arguments = _json.loads(arguments)
            except (ValueError, TypeError):
                arguments = {}

        user_id: int | None = details.get("user_id")
        voice_command: str = details.get("voice_command") or "[mobile chat]"

        ri = RequestInformation(
            voice_command=voice_command,
            conversation_id=tool_call_id or "mobile",
            user_id=user_id,
        )

        logger.info("Executing tool call", command=command_name, args=list(arguments.keys()), user_id=user_id)
        set_current_user_id(user_id)
        try:
            response = cmd.execute(ri, secrets=_build_secrets(cmd), **arguments)
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


def handle_routine(details: Dict[str, Any]) -> None:
    """Execute a whole routine on this node (run-now / scheduled) and POST result.

    CC publishes this for the mobile Run-now button and the scheduler. The whole
    routine runs through the unchanged RoutineCommand engine (sequential,
    error-resilient steps + LLM composition); the composed message + pass/fail
    counts are POSTed back to CC's /device-control-results/{request_id}.
    """
    reply_request_id: Optional[str] = details.get("reply_request_id")
    routine_name: str = details.get("routine_name", "")
    print(f"[MQTT] routine received: {routine_name} user_id={details.get('user_id')}", flush=True)

    if not reply_request_id:
        logger.warning("routine: missing reply_request_id, ignoring")
        return
    if not routine_name:
        logger.warning("routine: missing routine_name, ignoring")
        _post_tool_call_result(reply_request_id, {"output": {"error": "Missing routine_name", "success": False}})
        return

    try:
        from commands.routine_command import RoutineCommand
        from jarvis_command_sdk import RequestInformation
        from jarvis_command_sdk.context import set_current_user_id

        user_id: int | None = details.get("user_id")
        voice_command: str = details.get("voice_command") or f"routine: {routine_name}"
        ri = RequestInformation(
            voice_command=voice_command,
            conversation_id=reply_request_id,
            user_id=user_id,
        )

        logger.info("Executing routine", routine=routine_name, user_id=user_id)
        set_current_user_id(user_id)
        try:
            response = RoutineCommand().run(ri, routine_name=routine_name)
        finally:
            set_current_user_id(None)

        output: Dict[str, Any] = dict(response.context_data or {})
        if not response.success:
            output["error"] = response.error_details or "Routine failed"
        output["success"] = response.success

        logger.info("Routine completed", routine=routine_name, success=response.success)
        _post_tool_call_result(reply_request_id, {"output": output})

    except Exception as e:
        logger.error("Routine execution failed", routine=routine_name, error=str(e))
        _post_tool_call_result(reply_request_id, {"output": {"error": str(e), "success": False}})


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


# Identity / credential keys an over-the-wire config update must NEVER set.
# Endpoint URLs (``*_url``) and broker keys (``mqtt_*``) are matched by pattern
# below so new ones are covered automatically. Update/egress policy gates
# (UPDATE_POLICY_KEYS) are rejected here too: this channel is plaintext MQTT
# ferried by CC, so accepting them would let CC — or any broker publisher —
# flip a consent flag. They are writable only via the K2-encrypted config
# push (services/config_push_service, config_type "node_config").
_CONFIG_UPDATE_FORBIDDEN_KEYS = frozenset({"node_id", "api_key"})


def _reject_trust_anchor_keys(settings: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``settings`` with trust-anchor keys stripped out.

    ``update_node_config`` is a node-preference channel (volume, LEDs, mute,
    room, wake settings). It must not be able to write the command-center URL
    (which every verify-gate resolves the CC from → a repoint is node RCE), the
    node's identity/credentials, or the broker location. Reject those keys —
    identity by name, and every ``*_url`` endpoint / ``mqtt_*`` broker key by
    pattern — and log what was dropped.
    """
    safe: Dict[str, Any] = {}
    rejected: List[str] = []
    for key, value in settings.items():
        if (
            key in _CONFIG_UPDATE_FORBIDDEN_KEYS
            or key in UPDATE_POLICY_KEYS
            or key.endswith("_url")
            or key.startswith("mqtt_")
            # "_"-prefixed keys are config-file internals (e.g. the K2
            # node_config replay watermark) — never remotely writable.
            or key.startswith("_")
        ):
            rejected.append(key)
        else:
            safe[key] = value
    if rejected:
        logger.warning(
            "update_node_config: rejected trust-anchor/policy keys — a config "
            "push may not repoint the CC URL, node identity, or broker, nor "
            "flip update/egress consent gates",
            rejected=rejected,
        )
    return safe


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

    # A broker-delivered config update must not be able to repoint the CC URL
    # the verify-gates trust, or overwrite node identity / credentials / broker.
    # Strip those keys before anything is applied or persisted.
    settings = _reject_trust_anchor_keys(settings)
    if not settings:
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
        # Serialized + atomic: config.json has several concurrent writers
        # (this handler, the K2 node_config dispatcher, volume persist,
        # MQTT-cred persist); an unlocked read-modify-write here could
        # silently restore a policy key another writer just changed.
        update_config_file(lambda config: config.update(settings))

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
    raw_duration = details.get("duration_seconds", 3.0)
    try:
        duration = float(raw_duration if raw_duration is not None else 3.0)
        if duration != duration:  # NaN
            duration = 3.0
    except (TypeError, ValueError):
        logger.warning("preview_led_pattern: bad duration_seconds",
                       value=str(raw_duration))
        duration = 3.0
    # Clamp: an unbounded duration (buggy mobile build, redelivered QoS1
    # message) could park a purple wake_detected/alert preview for hours.
    clamped = max(0.5, min(30.0, duration))
    if clamped != duration:
        logger.warning("preview_led_pattern: duration clamped",
                       requested=duration, used=clamped)
    duration = clamped

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


def handle_device_removed(details: Dict[str, Any]) -> None:
    """CC published that a device was deleted — let its protocol clean up (e.g.
    HomeKit unpair) so the accessory is released and a future re-add starts fresh.
    Runs off-thread because protocol cleanup is async + may do network I/O.
    """
    def _run() -> None:
        import asyncio
        try:
            from services.direct_device_service import get_direct_device_service
            asyncio.run(get_direct_device_service().remove_device(details))
        except Exception as e:
            logger.warning("device_removed handler failed", error=str(e))

    _task_executor.submit(_run)


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
    "callback": handle_callback,
    "report_tools": handle_report_tools,
    "tool_call": handle_tool_call,
    "routine": handle_routine,
    "toggle_command": handle_toggle_command,
    "update_node_config": handle_update_node_config,
    "preview_led_pattern": handle_preview_led_pattern,
    "enroll_voice": handle_enroll_voice,
    "verify_voice": handle_verify_voice,
    "invalidate_device_cache": handle_invalidate_device_cache,
    "device_removed": handle_device_removed,
    "measure_ambient_noise": handle_measure_ambient_noise,
}


def _backstop_pending_settings_requests() -> None:
    """Fetch any still-pending settings requests from CC and process them.

    Backstops the QoS-1 + clean_session=False MQTT path for cases that
    broker persistence can't cover: broker restart during the offline
    window, or the first connect of a freshly-provisioned node (no
    pre-existing persistent session for the broker to queue against).

    Offloaded from on_connect because the paho callback expects fast
    return and a slow HTTP roundtrip would block other on_connect work.
    """
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    node_id: str = Config.get_str("node_id", "") or ""
    if not node_id:
        return
    cc_url: Optional[str] = get_command_center_url()
    if not cc_url:
        return

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/settings/requests"
    try:
        result = RestClient.get(url, timeout=10)
    except Exception as e:
        logger.warning("Pending-request backstop poll raised", error=str(e))
        return

    # CC returns a JSON array; RestClient.get may unwrap unexpectedly on
    # error. Be defensive about shape and treat anything weird as empty.
    if not isinstance(result, list) or not result:
        return
    logger.info("Pending settings requests found on reconnect", count=len(result))
    for item in result:
        if not isinstance(item, dict):
            continue
        req_id: str = item.get("request_id", "") or ""
        if not req_id:
            continue
        # We don't have include_values / user_id from the DB — those
        # only live in the original MQTT payload. Default to the
        # common mobile case (include_values=False, user_id=None).
        # The rare secret-sync flow that uses include_values=True
        # would need to retry from the mobile side if it lands here.
        _task_executor.submit(_process_settings_request, req_id, False, None)


def on_connect(client: mqtt.Client, userdata: Any, flags: Dict[str, int], rc: int) -> None:
    logger.info("MQTT connected", result_code=rc)
    topic = get_mqtt_config()["topic"]
    client.subscribe(topic, qos=1)
    logger.info("MQTT subscribed", topic=topic)

    # Subscribe to auth-ready notifications from JCC OAuth flow
    client.subscribe("jarvis/auth/+/ready", qos=1)
    logger.info("MQTT subscribed", topic="jarvis/auth/+/ready")

    # Backstop: pull any settings_requests that were created while we
    # were offline beyond broker persistence. Offload so the paho
    # callback returns immediately — the HTTP roundtrip is fine to
    # run on the shared task executor.
    _task_executor.submit(_backstop_pending_settings_requests)


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

    # The user who ran the OAuth flow owns any user-scoped tokens. Set the
    # SDK user ContextVar so store_auth_values() writes with scope="user"
    # resolve to their rows. None (older CC, admin flows) = legacy behavior.
    user_id: Optional[int] = result.pop("user_id", None)

    # Map JCC response keys to what store_auth_values() expects
    # JCC returns "base_url", commands expect "_base_url"
    if "base_url" in result and "_base_url" not in result:
        result["_base_url"] = result["base_url"]

    from jarvis_command_sdk import set_current_user_id

    # Find the command that owns this provider and store credentials
    service = get_command_discovery_service()
    commands = service.get_all_commands()

    for cmd in commands.values():
        if cmd.authentication and cmd.authentication.provider == provider:
            logger.info(
                "Storing auth credentials",
                provider=provider, command=cmd.command_name, user_id=user_id,
            )
            set_current_user_id(user_id)
            try:
                cmd.store_auth_values(result)
            finally:
                set_current_user_id(None)
            return

    # No command matched — check device families
    from utils.device_family_discovery_service import get_device_family_discovery_service

    family_service = get_device_family_discovery_service()
    families = family_service.get_all_families_for_snapshot()

    for family in families.values():
        if family.authentication and family.authentication.provider == provider:
            logger.info(
                "Storing auth credentials",
                provider=provider, family=family.protocol_name, user_id=user_id,
            )
            set_current_user_id(user_id)
            try:
                family.store_auth_values(result)
            finally:
                set_current_user_id(None)
            # Refresh discovery cache so next scan includes this family
            family_service.refresh()
            return

    logger.warning("No command or device family found for auth provider", provider=provider)


def _fetch_k2_from_cc(request_id: str) -> Dict[str, Any] | None:
    """Fetch the authoritative K2 key from CC over the node's authenticated
    (X-API-Key) channel. Returns the key payload ({k2, kid, created_at}) or None
    if CC has no pending K2 for this request_id — meaning the nudge was spoofed
    or stale, so nothing is saved.
    """
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url
    from utils.config_service import Config

    cc_url: str = get_command_center_url() or ""
    if not cc_url:
        logger.error("Cannot fetch K2: CC URL not resolved")
        return None

    node_id: str = Config.get_str("node_id", "") or ""
    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/k2/provision/{request_id}"
    result = RestClient.get(url, timeout=10)
    if not result or not result.get("k2"):
        return None
    return result


def _handle_k2_provision(raw_payload: bytes) -> None:
    """Handle a K2 provision nudge from CC (for Docker/headless nodes).

    Zero-trust: the MQTT message is an untrusted nudge carrying only a
    request_id. The actual K2 key material is fetched from CC over the node's
    authenticated (X-API-Key) channel — never taken from the MQTT payload — so a
    spoofed broker message cannot overwrite the node's settings-sync key (which
    would let an attacker decrypt pushed config and exfiltrate the node's
    outbound secret snapshot).
    """
    try:
        data: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in K2 provision message")
        return

    request_id: str = data.get("request_id", "")
    if not request_id:
        logger.warning("K2 provision nudge missing request_id")
        return

    key_data = _fetch_k2_from_cc(request_id)
    if not key_data:
        logger.warning("K2 provision verification failed", request_id=request_id[:8])
        _ack_k2_provision(request_id, success=False, error="verification failed")
        return

    k2 = key_data.get("k2", "")
    kid = key_data.get("kid", "")
    created_at = key_data.get("created_at", "")
    if not k2 or not kid:
        logger.warning("K2 provision fetch returned incomplete key")
        _ack_k2_provision(request_id, success=False, error="incomplete key data")
        return

    try:
        from datetime import datetime
        from utils.encryption_utils import save_k2

        dt = datetime.fromisoformat(created_at) if created_at else datetime.utcnow()
        save_k2(k2, kid, dt)
        logger.info("K2 encryption key provisioned via CC pull", kid=kid)
        print(f"[MQTT] K2 provisioned (verified): kid={kid}", flush=True)
        _ack_k2_provision(request_id, success=True)
    except Exception as e:
        logger.error("K2 provisioning failed", error=str(e))
        print(f"[MQTT] K2 provision error: {e}", flush=True)
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


def _handle_routines_sync_notification(raw_payload: bytes) -> None:
    """Handle a routines/sync nudge from CC — pull the household routine set.

    The nudge carries no routine data; we pull the full set over the node's
    authenticated HTTP channel and rewrite the DB layer. Already running on the
    task executor (via _offload), so the HTTP roundtrip is safe here.
    """
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
        logger.info("Routines sync nudge received", event=notification.get("event"))
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in routines sync nudge")
        return

    try:
        from services.routine_sync_service import pull_routines

        count = pull_routines()
        logger.info("Routines sync complete", count=count)
    except Exception as e:
        logger.error("Routines sync failed", error=str(e))


def _handle_command_data_topic(request_topic: str, raw_payload: bytes, op: str) -> None:
    """Dispatch a mobile command-data browser request to the handler module.

    Runs on the shared task executor. Response publishing happens inside
    the handler, which uses the global `_mqtt_client` reference."""
    if _mqtt_client is None:
        logger.warning(
            "command-data request arrived before MQTT client ready",
            topic=request_topic,
            op=op,
        )
        return
    from services.command_data_handler import handle_command_data_request
    handle_command_data_request(_mqtt_client, request_topic, raw_payload, op)


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
    """Handle a package install nudge from CC — verify with CC, then install.

    Zero-trust: this MQTT message is an untrusted nudge. Only ``request_id`` is
    used; the authoritative repo URL is fetched from CC's verify endpoint (which
    404s on a spoofed request_id). We never install from a URL supplied in the
    MQTT payload — that would let anyone able to reach the broker install
    arbitrary code on the node.
    """
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in package install notification")
        return

    request_id: str = notification.get("request_id", "")
    if not request_id:
        print(f"[INSTALL] missing request_id, ignoring", flush=True)
        return

    print(f"[INSTALL] nudge received request_id={request_id[:8]}", flush=True)

    from services.package_install_handler import run_install_and_upload

    _task_executor.submit(run_install_and_upload, request_id)
    print("[INSTALL] task submitted", flush=True)


def _handle_package_uninstall_notification(raw_payload: bytes) -> None:
    """Handle a package uninstall nudge from CC — verify with CC, then remove.

    Zero-trust: the package to remove is taken from CC's verify response (keyed
    on a request_id CC issued for this node), not the MQTT payload, so a forged
    nudge cannot remove an arbitrary package. component_type is only a narrowing
    hint (it scopes removal within the CC-authoritative package).
    """
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in package uninstall notification")
        return

    request_id: str = notification.get("request_id", "")
    component_type: str | None = notification.get("component_type")

    if not request_id:
        print("[UNINSTALL] missing request_id, ignoring", flush=True)
        return

    print(f"[UNINSTALL] nudge received request_id={request_id[:8]}", flush=True)

    from services.package_install_handler import run_uninstall_and_upload

    _task_executor.submit(run_uninstall_and_upload, request_id, component_type)
    print("[UNINSTALL] task submitted", flush=True)


def _handle_package_revert_notification(raw_payload: bytes) -> None:
    """Handle a package revert nudge from CC — verify with CC, then revert.

    Zero-trust: the package to revert is taken from CC's verify response, not
    the MQTT payload, so a forged nudge cannot revert an arbitrary package.
    """
    try:
        notification: Dict[str, Any] = json.loads(raw_payload.decode())
    except json.JSONDecodeError:
        logger.warning("Invalid JSON in package revert notification")
        return

    request_id: str = notification.get("request_id", "")

    if not request_id:
        print("[REVERT] missing request_id, ignoring", flush=True)
        return

    print(f"[REVERT] nudge received request_id={request_id[:8]}", flush=True)

    from services.package_install_handler import run_revert_and_upload

    _task_executor.submit(run_revert_and_upload, request_id)
    print("[REVERT] task submitted", flush=True)


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


def _offload(handler: Callable[..., Any], *args: Any) -> None:
    """Submit a handler to the task executor, wrapped to log + swallow errors.

    Everything dispatched here MUST be offloaded — running handlers
    synchronously on the MQTT loop thread blocks PINGREQ and triggers
    broker-side keepalive disconnects under any sustained workload.
    Settings snapshots, bluetooth scans, package installs all routinely
    take seconds; the loop thread can't afford that.
    """
    def _run() -> None:
        try:
            handler(*args)
        except Exception as e:
            logger.error(
                "MQTT topic handler failed",
                handler=getattr(handler, "__name__", str(handler)),
                error=str(e),
            )
    _task_executor.submit(_run)


def on_message(client: mqtt.Client, userdata: Any, msg: mqtt.MQTTMessage) -> None:
    print(f"[MQTT] message on {msg.topic}", flush=True)
    # All topic-based dispatch goes through _offload — handlers run on
    # the shared task executor, on_message returns immediately so the
    # MQTT loop thread can keep PINGREQ-ing on time.

    if msg.topic.startswith("jarvis/auth/") and msg.topic.endswith("/ready"):
        _offload(_handle_auth_ready, msg.payload)
        return

    if msg.topic.endswith("/k2/provision"):
        _offload(_handle_k2_provision, msg.payload)
        return

    if msg.topic.endswith("/config/push"):
        _offload(_handle_config_push_notification, msg.payload)
        return

    if msg.topic.endswith("/routines/sync"):
        _offload(_handle_routines_sync_notification, msg.payload)
        return

    if msg.topic.endswith("/settings/request"):
        _offload(_handle_settings_request_notification, msg.payload)
        return

    if msg.topic.endswith("/device-list"):
        _offload(_handle_device_list_notification, msg.payload)
        return

    if msg.topic.endswith("/bluetooth-scan"):
        _offload(_handle_bluetooth_scan_notification, msg.payload)
        return

    if msg.topic.endswith("/bluetooth-pair"):
        _offload(_handle_bluetooth_pair_notification, msg.payload)
        return

    if msg.topic.endswith("/bluetooth-disconnect"):
        _offload(_handle_bluetooth_disconnect_notification, msg.payload)
        return

    if msg.topic.endswith("/bluetooth-discoverable"):
        _offload(_handle_bluetooth_discoverable_notification, msg.payload)
        return

    if msg.topic.endswith("/bluetooth-release"):
        _offload(_handle_bluetooth_release_notification, msg.payload)
        return

    if msg.topic.endswith("/bluetooth-auto-connect"):
        _offload(_handle_bluetooth_auto_connect_notification, msg.payload)
        return

    if msg.topic.endswith("/device-scan"):
        _offload(_handle_device_scan_notification, msg.payload)
        return

    if msg.topic.endswith("/device-state"):
        _offload(_handle_device_state_notification, msg.payload)
        return

    if msg.topic.endswith("/camera-credentials"):
        _offload(_handle_camera_credentials_notification, msg.payload)
        return

    if msg.topic.endswith("/test-install"):
        _offload(_handle_test_install_notification, msg.payload)
        return

    if msg.topic.endswith("/package-install"):
        _offload(_handle_package_install_notification, msg.payload)
        return

    if msg.topic.endswith("/package-uninstall"):
        _offload(_handle_package_uninstall_notification, msg.payload)
        return

    if msg.topic.endswith("/package-revert"):
        _offload(_handle_package_revert_notification, msg.payload)
        return

    if msg.topic.endswith("/factory-reset"):
        _offload(_handle_factory_reset, msg.payload)
        return

    if "/command-data/" in msg.topic and "/response/" not in msg.topic:
        op = msg.topic.rsplit("/command-data/", 1)[-1]
        # Derive the routable ops from the handler so the two never drift
        # (cached after first import; the handler module is already loaded).
        from services.command_data_handler import SUPPORTED_OPS
        if op in SUPPORTED_OPS:
            _offload(_handle_command_data_topic, msg.topic, msg.payload, op)
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
            # Offload to the task executor so the MQTT loop thread returns
            # immediately and stays available for PINGREQ. Running heavy
            # handlers (report_tools does discovery + HTTP POST, package-
            # install spawns subprocesses) inline blocked the loop thread
            # for long enough that the broker timed the client out under
            # the prior 60s keepalive — manifesting as CONN_LOST drops
            # every 1-3 minutes during active sessions.
            _task_executor.submit(_run_command_handler, command, handler, details)
        else:
            logger.warning("Unknown MQTT command", command=command)


def _run_command_handler(
    command: str,
    handler: Callable[[Dict[str, Any]], None],
    details: Dict[str, Any],
) -> None:
    """Wrapper that runs an MQTT command handler in a background thread.

    Keeps the MQTT loop thread free to send PINGREQs and process new
    messages. Errors are logged but never raised — the executor's pool
    workers must stay healthy to handle future commands.
    """
    try:
        handler(details)
    except Exception as e:
        logger.error("Error running MQTT handler", command=command, error=str(e))


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

    # Resolve the broker for initial client setup. No localhost fallback: if
    # config-service can't resolve the broker yet (node booted before the stack,
    # or a transient outage), retry with capped backoff and re-init discovery
    # each round until we get the real address or shutdown — never fabricate
    # localhost. Runs in the MQTT thread, so blocking here never stalls the node.
    from utils.service_discovery import ServiceUnresolvedError
    from utils.service_discovery import is_initialized as _sd_is_initialized
    from utils.service_discovery import init as _sd_init

    config: Optional[Dict[str, Any]] = None
    _init_attempt: int = 0
    while _shutdown_event is None or not _shutdown_event.is_set():
        _init_attempt += 1
        if not _sd_is_initialized():
            _sd_init()
        try:
            config = get_mqtt_config()
            break
        except ServiceUnresolvedError as e:
            _init_backoff: int = min(2 ** min(_init_attempt, 6), 60)
            logger.warning(
                "MQTT broker not resolvable from config-service yet, retrying",
                attempt=_init_attempt,
                retry_in_seconds=_init_backoff,
                error=str(e),
            )
            if _shutdown_event is not None:
                if _shutdown_event.wait(timeout=_init_backoff):
                    return
            else:
                time.sleep(_init_backoff)
    if config is None:
        # Shutdown requested before the broker ever resolved.
        return

    # If the broker requires auth but this node has no credential yet (the
    # transition window), fetch it from command-center over the authenticated
    # HTTP channel and persist it. Falls through to anonymous if command-center
    # has no credential yet or is unreachable — connecting exactly as today.
    if not config["username"] or not config["password"]:
        fetched_user, fetched_pw = fetch_and_persist_mqtt_credentials()
        if fetched_user and fetched_pw:
            config["username"] = fetched_user
            config["password"] = fetched_pw

    scheme: str = config["scheme"]
    transport: str = "websockets" if scheme in ("ws", "wss") else "tcp"

    # Stable client_id + persistent session so the broker queues QoS-1
    # messages for us across disconnects. Without ``clean_session=False``
    # (and a stable ID the broker can recognize), every flap drops the
    # session and any in-flight settings_request / config_push / TTS
    # message published while we were offline is gone forever — the
    # ~30s of voice-settings-not-loading we hit during beta. If node_id
    # isn't set yet (un-provisioned node), fall back to paho's random
    # ID with clean_session=True; the MQTT spec rejects empty client_id
    # with clean_session=False so we can't persist that case.
    node_id_for_client: str = Config.get_str("node_id", "") or ""
    if node_id_for_client:
        client_id_arg: str = f"jarvis-node-{node_id_for_client}"
        clean_session_arg: bool = False
    else:
        client_id_arg = ""  # paho generates a random one
        clean_session_arg = True

    client: mqtt.Client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION1,
        client_id=client_id_arg,
        clean_session=clean_session_arg,
        transport=transport,
    )
    logger.info(
        "MQTT client init",
        client_id=client_id_arg or "<random>",
        clean_session=clean_session_arg,
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

    # NOTE: do NOT call client.enable_logger(jarvis_logger) — paho expects a
    # stdlib logging.Logger with .log(level, msg, *args) semantics; our
    # JarvisLogger has a kwargs-style API, and the mismatch raises inside
    # paho's connect path → MQTT thread crashes → supervisor restart loop.
    # If we need PINGREQ visibility later, route through a real
    # logging.Logger (logging.getLogger("paho-mqtt")) instead.

    logger.info(
        "MQTT listener starting",
        scheme=scheme,
        broker=config["broker"],
        port=config["port"],
        transport=transport,
    )

    # Connect with backed-off retries that RE-RESOLVE the broker each round and
    # re-attempt service discovery. A node that booted before config-service was
    # ready (e.g. a full-stack restart) first resolves the localhost fallback,
    # which reaches nothing inside a container. The old code retried that dead
    # address a fixed 5 times and then gave up ("continuing without MQTT") —
    # leaving the node permanently dark on MQTT until a manual restart, because
    # service discovery is only initialised once at boot and was never retried.
    # Here we re-init discovery and re-resolve every round, so the node is
    # promoted to the authoritative broker URL as soon as config-service comes
    # up, and we keep trying (capped backoff) until connected or shutting down.
    # Runs in the MQTT thread, so blocking here never holds up the rest of the
    # node. Keepalive 120s: the Pi can go silent >60s under GIL contention
    # (wake-word + audio); 120s gives the broker 180s tolerance (1.5x keepalive).
    from utils.service_discovery import is_initialized as _sd_is_initialized
    from utils.service_discovery import init as _sd_init

    attempt: int = 0
    while _shutdown_event is None or not _shutdown_event.is_set():
        attempt += 1
        # init() is a no-op once initialised; retrying it lets a node that
        # started ahead of config-service discover the real broker instead of
        # staying stuck on the localhost fallback.
        if not _sd_is_initialized():
            _sd_init()
        broker_host, broker_port = None, None
        try:
            current = get_mqtt_config()
            broker_host, broker_port = current["broker"], current["port"]
            client.connect(broker_host, broker_port, 120)
            logger.info(
                "MQTT connecting", broker=broker_host, port=broker_port, attempt=attempt
            )
            break
        except (ConnectionRefusedError, OSError, ServiceUnresolvedError) as e:
            backoff: int = min(2 ** min(attempt, 6), 60)
            logger.warning(
                "MQTT connection attempt failed, retrying",
                broker=broker_host,
                port=broker_port,
                attempt=attempt,
                retry_in_seconds=backoff,
                error=str(e),
            )
            if _shutdown_event is not None:
                if _shutdown_event.wait(timeout=backoff):
                    logger.info("Shutdown requested during MQTT connect retries")
                    return
            else:
                time.sleep(backoff)
    else:
        # Loop condition went false (shutdown) before we ever connected.
        return

    _mqtt_client = client
    client.loop_forever()
