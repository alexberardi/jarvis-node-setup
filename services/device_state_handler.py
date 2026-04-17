"""Handle device state requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/device-state with {request_id, entity_id, ...}
2. This handler queries the adapter's get_state() or HA get_state()
3. Normalizes via DomainHandler and POSTs result back to CC
"""

import asyncio
from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from device_families.domains import UIControlHints, get_domain_handler
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


def run_state_query_and_upload(request_id: str, details: dict[str, Any]) -> None:
    """Run device state query and upload results to CC. Runs in a background thread."""
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_async_query_and_upload(request_id, details))
    except Exception as e:
        logger.error("Device state handler failed", request_id=request_id[:8], error=str(e))
        _upload_result(request_id, {"error": str(e)})
    finally:
        loop.close()


async def _async_query_and_upload(request_id: str, details: dict[str, Any]) -> None:
    """Async: query device state, normalize, and POST result to CC."""
    entity_id: str = details.get("entity_id", "")
    source: str = details.get("source", "direct")
    domain: str = details.get("domain", "")

    if not entity_id:
        _upload_result(request_id, {"error": "missing entity_id"})
        return

    # Infer domain from entity_id if not provided
    if not domain and "." in entity_id:
        domain = entity_id.split(".", 1)[0]

    raw_state: dict[str, Any] | None = None

    if source == "direct":
        raw_state = await _query_direct_device(details)
    else:
        raw_state = await _query_ha_device(entity_id)

    if raw_state is None:
        _upload_result(request_id, {
            "entity_id": entity_id,
            "domain": domain,
            "error": "Failed to query device state",
        })
        return

    # Normalize via domain handler
    handler = get_domain_handler(domain)
    if handler:
        normalized = handler.normalize_state(raw_state)
        # Pass device-reported modes (e.g. Nest available_modes) to UI hints
        available = [m.lower() for m in raw_state.get("available_modes", [])]
        ui_hints = handler.get_ui_hints(features=available if available else None)
    else:
        normalized = raw_state
        ui_hints = UIControlHints(control_type="toggle")

    result: dict[str, Any] = {
        "entity_id": entity_id,
        "domain": domain,
        "state": normalized,
        "ui_hints": {
            "control_type": ui_hints.control_type,
            "features": ui_hints.features,
            "min_value": ui_hints.min_value,
            "max_value": ui_hints.max_value,
            "step": ui_hints.step,
            "unit": ui_hints.unit,
        },
    }

    logger.info("Device state queried", entity_id=entity_id, domain=domain)
    _upload_result(request_id, result)


async def _query_direct_device(details: dict[str, Any]) -> dict[str, Any] | None:
    """Query state from a direct WiFi device via its protocol adapter."""
    entity_id: str = details.get("entity_id", "")
    try:
        from utils.device_family_discovery_service import get_device_family_discovery_service

        discovery = get_device_family_discovery_service()
        protocol_name: str = details.get("protocol", "")
        adapter = discovery.get_family(protocol_name) if protocol_name else None

        if adapter:
            return await adapter.get_state(
                ip=details.get("local_ip", ""),
                entity_id=entity_id,
                cloud_id=details.get("cloud_id", ""),
                mac_address=details.get("mac_address", ""),
            )
    except ImportError:
        logger.debug("DirectDeviceService not available")
    except Exception as e:
        logger.warning("Direct device state query failed", entity_id=entity_id, error=str(e))

    return None


async def _query_ha_device(entity_id: str) -> dict[str, Any] | None:
    """Query state from Home Assistant."""
    try:
        from ha_shared.home_assistant_service import HomeAssistantService

        service = HomeAssistantService()
        result = await service.get_state(entity_id)
        if result.success:
            state_dict: dict[str, Any] = {"state": result.state}
            if result.attributes:
                state_dict.update(result.attributes)
            return state_dict
    except (ImportError, ValueError) as e:
        logger.debug("HA state query unavailable", error=str(e))
    except Exception as e:
        logger.warning("HA state query failed", entity_id=entity_id, error=str(e))

    return None


def _upload_result(request_id: str, data: dict[str, Any]) -> None:
    """POST state result to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload state result: CC URL not resolved")
        return

    url = f"{cc_url.rstrip('/')}/api/v0/device-state-results/{request_id}"
    result = RestClient.post(url, data=data, timeout=10)
    if result:
        logger.debug("State result uploaded to CC", request_id=request_id[:8])
    else:
        logger.error("Failed to upload state result to CC", request_id=request_id[:8])
