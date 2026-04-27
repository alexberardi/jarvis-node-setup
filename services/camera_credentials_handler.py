"""Handle camera credential requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/camera-credentials with {request_id, protocol, ...}
2. This handler reads credentials from the protocol's JarvisStorage
3. POSTs credentials back to CC at /api/v0/camera-credentials/{request_id}
"""

from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

# Protocol → list of secret keys needed for camera streaming
_PROTOCOL_CREDENTIAL_KEYS: dict[str, list[str]] = {
    "nest": [
        "NEST_REFRESH_TOKEN",
        "NEST_WEB_CLIENT_ID",
        "NEST_WEB_CLIENT_SECRET",
        "NEST_PROJECT_ID",
    ],
}

# Protocol → mapping from secret key to response field name
_PROTOCOL_FIELD_MAP: dict[str, dict[str, str]] = {
    "nest": {
        "NEST_REFRESH_TOKEN": "refresh_token",
        "NEST_WEB_CLIENT_ID": "client_id",
        "NEST_WEB_CLIENT_SECRET": "client_secret",
        "NEST_PROJECT_ID": "project_id",
    },
}


def run_credentials_lookup_and_upload(request_id: str, details: dict[str, Any]) -> None:
    """Look up camera credentials and upload to CC. Runs in a background thread."""
    protocol: str = details.get("protocol", "")

    if not protocol:
        _upload_result(request_id, {"error": "missing protocol"})
        return

    if protocol not in _PROTOCOL_CREDENTIAL_KEYS:
        _upload_result(request_id, {"error": f"unsupported camera protocol: {protocol}"})
        return

    try:
        from services.secret_service import get_secret_value

        keys = _PROTOCOL_CREDENTIAL_KEYS[protocol]
        field_map = _PROTOCOL_FIELD_MAP[protocol]
        result: dict[str, str] = {}
        missing: list[str] = []

        for key in keys:
            value: str | None = get_secret_value(key, "integration")
            if value:
                result[field_map[key]] = value
            else:
                missing.append(key)

        if missing:
            _upload_result(request_id, {
                "error": f"Missing credentials: {', '.join(missing)}. Complete OAuth setup in Node Settings.",
            })
            return

        logger.info("Camera credentials retrieved", protocol=protocol, request_id=request_id[:8])
        _upload_result(request_id, result)

    except Exception as e:
        logger.error("Camera credentials lookup failed", request_id=request_id[:8], error=str(e))
        _upload_result(request_id, {"error": str(e)})


def _upload_result(request_id: str, data: dict[str, Any]) -> None:
    """POST credential result to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload credentials: CC URL not resolved")
        return

    url = f"{cc_url.rstrip('/')}/api/v0/camera-credentials/{request_id}"
    result = RestClient.post(url, data=data, timeout=10)
    if result:
        logger.debug("Camera credentials uploaded to CC", request_id=request_id[:8])
    else:
        logger.error("Failed to upload camera credentials to CC", request_id=request_id[:8])
