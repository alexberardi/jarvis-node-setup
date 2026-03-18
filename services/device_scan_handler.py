"""Handle user-driven device scan requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/device-scan with {request_id}
2. This handler runs protocol adapters (LIFX, Kasa, Govee, etc.)
3. Results are POSTed back to CC for mobile to poll
"""

import asyncio
from dataclasses import asdict
from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from device_families.base import DiscoveredDevice
from utils.device_family_discovery_service import get_device_family_discovery_service
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

SCAN_TIMEOUT: float = 10.0


def run_scan_and_upload(request_id: str) -> None:
    """Run device scan and upload results to CC. Meant to run in a background thread."""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_async_scan_and_upload(request_id))
    except Exception as e:
        logger.error("Device scan handler failed", request_id=request_id[:8], error=str(e))
        _upload_error(request_id, str(e))
    finally:
        loop.close()


async def _async_scan_and_upload(request_id: str) -> None:
    """Async: discover devices via protocol adapters and POST results to CC."""
    discovery = get_device_family_discovery_service()
    families = discovery.get_all_families()

    if not families:
        logger.info("No device families available for scan", request_id=request_id[:8])
        _upload_results(request_id, [])
        return

    protocols = list(families.values())
    scan_tasks = [p.discover(timeout=SCAN_TIMEOUT) for p in protocols]
    results = await asyncio.gather(*scan_tasks, return_exceptions=True)

    all_devices: list[DiscoveredDevice] = []
    for i, result in enumerate(results):
        protocol_name = protocols[i].protocol_name
        if isinstance(result, Exception):
            logger.error("Protocol scan failed", protocol=protocol_name, error=str(result))
            continue
        logger.info("Protocol scan complete", protocol=protocol_name, device_count=len(result))
        all_devices.extend(result)

    # Deduplicate (same logic as DeviceScannerService.scan)
    by_key: dict[str, DiscoveredDevice] = {}
    for dev in all_devices:
        if dev.mac_address:
            by_key[dev.mac_address.lower()] = dev
        elif dev.local_ip:
            by_key[dev.local_ip] = dev
        elif dev.cloud_id:
            by_key[dev.cloud_id] = dev
        else:
            by_key[dev.entity_id] = dev

    unique = list(by_key.values())
    logger.info("Device scan complete", request_id=request_id[:8], device_count=len(unique))

    _upload_results(request_id, unique)


def _upload_results(request_id: str, devices: list[DiscoveredDevice]) -> None:
    """POST scan results to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload scan results: CC URL not resolved")
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/device-scan/{request_id}/results"

    # Serialize DiscoveredDevice to dicts
    device_dicts: list[dict[str, Any]] = []
    for dev in devices:
        d = asdict(dev)
        d.pop("extra", None)  # Don't send internal extra data
        device_dicts.append(d)

    result = RestClient.post(url, data={"devices": device_dicts}, timeout=15)
    if result:
        logger.info("Scan results uploaded to CC", request_id=request_id[:8], device_count=len(devices))
    else:
        logger.error("Failed to upload scan results to CC", request_id=request_id[:8])


def _upload_error(request_id: str, error_message: str) -> None:
    """POST error to CC for a failed scan."""
    cc_url = get_command_center_url()
    if not cc_url:
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/device-scan/{request_id}/results"
    RestClient.post(url, data={"devices": [], "error": error_message}, timeout=10)
