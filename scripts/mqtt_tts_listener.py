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

# Global MQTT client reference for publishing replies
_mqtt_client: Optional[mqtt.Client] = None


def get_mqtt_config() -> Dict[str, Any]:
    """Get MQTT configuration at runtime.

    Discovery chain:
    1. Config-service (jarvis-mqtt-broker)
    2. Env vars (JARVIS_MQTT_BROKER, JARVIS_MQTT_PORT)
    3. config.json (mqtt_broker, mqtt_port)
    4. Default: localhost:1884
    """
    from utils.service_discovery import get_mqtt_broker_url

    node_id: str = Config.get_str("node_id", "unknown") or "unknown"

    # Parse broker URL from service discovery
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
        _post_action_result(reply_id, response.success, error_msg)

        # TTS only for voice-originated actions (no reply_request_id)
        if message and not details.get("reply_request_id"):
            tts_provider = get_tts_provider()
            tts_provider.speak(True, message)
    except Exception as e:
        logger.error("Action handler error", command=command_name, action=action_name, error=str(e))
        reply_id_err: str = details.get("reply_request_id") or request_id
        _post_action_result(reply_id_err, False, str(e))


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


command_handlers: Dict[str, Callable[[Dict[str, Any]], None]] = {
    "tts": handle_tts,
    "train_adapter": handle_train_adapter,
    "action": handle_action,
    "report_tools": handle_report_tools,
    "tool_call": handle_tool_call,
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


def _handle_device_protocol_control(action_name: str, context: Dict[str, Any], reply_id: str) -> None:
    """Dispatch device control directly to a device protocol (no command needed).

    Used when the control_device command isn't installed (e.g. standalone
    Govee/Nest protocols without the HA integration bundle).
    """
    import asyncio

    protocol_name: str = context.get("protocol", "")
    entity_id: str = context.get("entity_id", "")

    if not protocol_name:
        _post_action_result(reply_id, False, "No protocol specified in device context")
        return

    try:
        from utils.device_family_discovery_service import get_device_family_discovery_service
        from jarvis_command_sdk import DiscoveredDevice

        svc = get_device_family_discovery_service()
        svc.discover_families()
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

        # Run async control
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(protocol.control(device, action_name, context))
            print(f"[ACTION] device protocol control: {protocol_name} {action_name} success={result.success}", flush=True)
            input_req = result.input_required.to_dict() if result.input_required else None
            _post_action_result(reply_id, result.success, result.error if not result.success else None, input_required=input_req)
        finally:
            loop.close()

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
    """Periodically POST heartbeat to command center to update last_seen."""
    import time
    from clients.rest_client import RestClient
    from utils.service_discovery import get_command_center_url

    # Initial delay: let service discovery initialize
    time.sleep(10)

    while True:
        try:
            base_url: str = get_command_center_url() or ""
            if base_url:
                url = f"{base_url.rstrip('/')}/api/v0/admin/nodes/heartbeat"
                RestClient.post(url, data={}, timeout=10)
        except Exception:
            pass  # Heartbeat is best-effort, retries next interval
        time.sleep(_HEARTBEAT_INTERVAL_SECONDS)


_TEST_CLEANUP_INTERVAL_SECONDS = 1200  # 20 minutes


def _test_command_cleanup_loop() -> None:
    """Periodically clean up expired test commands."""
    import time

    time.sleep(60)  # Initial delay to let services start
    while True:
        try:
            from services.test_install_cleanup import cleanup_expired_test_commands
            count = cleanup_expired_test_commands()
            if count:
                logger.info("Test command cleanup cycle complete", removed=count)
        except Exception:
            pass  # Best-effort, retries next cycle
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

    logger.info("MQTT listener starting", broker=config["broker"], port=config["port"])
    try:
        client.connect(config["broker"], config["port"], 60)
    except (ConnectionRefusedError, OSError) as e:
        logger.warning("MQTT broker not reachable, continuing without MQTT", broker=config["broker"], error=str(e))
        return
    _mqtt_client = client
    client.loop_forever()
