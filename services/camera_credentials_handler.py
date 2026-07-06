"""Handle camera stream-source requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/camera-credentials with
   {request_id, protocol, cloud_id, entity_id, domain}
2. This handler resolves the device-protocol plugin for `protocol` and asks it
   to build a go2rtc source string via IJarvisDeviceProtocol.get_stream_source()
3. POSTs {stream_source: "..."} (or {error: "..."}) back to CC at
   /api/v0/camera-credentials/{request_id}

The node — specifically the device-protocol plugin — owns the go2rtc source
format and the choice of streaming transport (WebRTC vs RTSP). Command-center
registers whatever source string this returns, verbatim; it holds no protocol
specifics.
"""

import asyncio
from typing import Any

from jarvis_command_sdk import DiscoveredDevice
from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


def run_credentials_lookup_and_upload(request_id: str, details: dict[str, Any]) -> None:
    """Build a go2rtc stream source and upload it to CC. Runs in a background thread."""
    try:
        # asyncio.run() — NOT a hand-rolled new_event_loop()/loop.close() — so any
        # ThreadPoolExecutor a protocol spins up (e.g. httpx wrapped via to_thread)
        # is shut down rather than leaked (the 2026-06-23 "can't start new thread"
        # node-death mode). See device_state_handler for the same pattern.
        asyncio.run(_async_build_and_upload(request_id, details))
    except Exception as e:
        logger.error("Camera stream source lookup failed", request_id=request_id[:8], error=str(e))
        _upload_result(request_id, {"error": str(e)})


async def _async_build_and_upload(request_id: str, details: dict[str, Any]) -> None:
    """Resolve the protocol plugin, build the go2rtc source, POST it to CC."""
    protocol: str = details.get("protocol", "")
    if not protocol:
        _upload_result(request_id, {"error": "missing protocol"})
        return

    cloud_id: str = details.get("cloud_id", "")
    entity_id: str = details.get("entity_id", "")
    domain: str = details.get("domain", "camera")

    from utils.device_family_discovery_service import get_device_family_discovery_service

    adapter = get_device_family_discovery_service().get_family(protocol)
    if adapter is None:
        _upload_result(request_id, {"error": f"protocol not available: {protocol}"})
        return

    build_stream_source = getattr(adapter, "get_stream_source", None)
    if build_stream_source is None:
        # Older SDK without the get_stream_source hook.
        _upload_result(request_id, {"error": f"stream source not supported by protocol: {protocol}"})
        return

    device = DiscoveredDevice(
        name=entity_id or "camera",
        domain=domain,
        manufacturer="",
        model="",
        protocol=protocol,
        entity_id=entity_id,
        cloud_id=cloud_id,
    )

    stream_source = await build_stream_source(device)
    if not stream_source:
        _upload_result(request_id, {
            "error": "Camera not configured or not supported. Complete OAuth setup in Node Settings.",
        })
        return

    logger.info("Camera stream source built", protocol=protocol, request_id=request_id[:8])
    _upload_result(request_id, {"stream_source": stream_source})


def _upload_result(request_id: str, data: dict[str, Any]) -> None:
    """POST the stream-source result to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload camera stream source: CC URL not resolved")
        return

    url = f"{cc_url.rstrip('/')}/api/v0/camera-credentials/{request_id}"
    result = RestClient.post(url, data=data, timeout=10)
    if result:
        logger.debug("Camera stream source uploaded to CC", request_id=request_id[:8])
    else:
        logger.error("Failed to upload camera stream source to CC", request_id=request_id[:8])
