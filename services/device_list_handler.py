"""Handle device-list requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/device-list with {request_id, manager_name}
2. This handler runs the selected IJarvisDeviceManager.collect_devices()
3. Results are POSTed back to CC for mobile to poll

Mirrors device_scan_handler.py.
"""

import asyncio
from dataclasses import asdict
from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.device_manager_discovery_service import get_device_manager_discovery_service
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


def run_collect_and_upload(request_id: str, manager_name: str) -> None:
    """Run device collection and upload results to CC.  Meant to run in a background thread."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_async_collect_and_upload(request_id, manager_name))
    except Exception as e:
        logger.error(
            "Device list handler failed",
            request_id=request_id[:8],
            manager=manager_name,
            error=str(e),
        )
        _upload_error(request_id, str(e))
    finally:
        loop.close()


async def _async_collect_and_upload(request_id: str, manager_name: str) -> None:
    """Async: collect devices via the selected manager and POST results to CC."""
    discovery = get_device_manager_discovery_service()
    manager = discovery.get_manager(manager_name)

    if manager is None:
        error_msg = f"Device manager '{manager_name}' not found or not configured"
        logger.warning(error_msg, request_id=request_id[:8])
        _upload_error(request_id, error_msg)
        return

    logger.info(
        "Collecting device list",
        request_id=request_id[:8],
        manager=manager_name,
    )

    devices = await manager.collect_devices()

    logger.info(
        "Device list collected",
        request_id=request_id[:8],
        manager=manager_name,
        device_count=len(devices),
    )

    _upload_results(request_id, manager, devices)


def _upload_results(
    request_id: str,
    manager: Any,
    devices: list[Any],
) -> None:
    """POST device list results to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload device list results: CC URL not resolved")
        return

    from utils.config_service import Config

    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/device-list/{request_id}/results"

    device_dicts: list[dict[str, Any]] = []
    for dev in devices:
        d = asdict(dev)
        d.pop("extra", None)
        device_dicts.append(d)

    payload: dict[str, Any] = {
        "devices": device_dicts,
        "manager_name": manager.name,
        "can_edit_devices": manager.can_edit_devices,
    }

    result = RestClient.post(url, data=payload, timeout=15)
    if result:
        logger.info(
            "Device list results uploaded to CC",
            request_id=request_id[:8],
            device_count=len(devices),
        )
    else:
        logger.error("Failed to upload device list results to CC", request_id=request_id[:8])


def _upload_error(request_id: str, error_message: str) -> None:
    """POST error to CC for a failed device list request."""
    cc_url = get_command_center_url()
    if not cc_url:
        return

    from utils.config_service import Config

    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/device-list/{request_id}/results"
    RestClient.post(url, data={"devices": [], "error": error_message}, timeout=10)
