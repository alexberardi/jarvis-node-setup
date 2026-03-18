"""Jarvis Direct device manager — aggregates devices from IJarvisDeviceProtocol adapters.

Wraps DeviceFamilyDiscoveryService and runs each configured protocol adapter's
discover() in parallel, deduplicates, and returns a normalized list.
"""

import asyncio

from jarvis_log_client import JarvisLogger

from core.ijarvis_device_manager import DeviceManagerDevice, IJarvisDeviceManager
from device_families.base import DiscoveredDevice
from utils.device_family_discovery_service import get_device_family_discovery_service

logger = JarvisLogger(service="jarvis-node")

SCAN_TIMEOUT: float = 10.0


class JarvisDirectDeviceManager(IJarvisDeviceManager):
    """Discovers devices directly via LAN/cloud protocol adapters (LIFX, Kasa, etc.)."""

    @property
    def name(self) -> str:
        return "jarvis_direct"

    @property
    def friendly_name(self) -> str:
        return "Jarvis Direct"

    @property
    def description(self) -> str:
        return "WiFi devices controlled directly (LIFX, Kasa, Govee, etc.)"

    @property
    def can_edit_devices(self) -> bool:
        return True

    async def collect_devices(self) -> list[DeviceManagerDevice]:
        """Run all configured protocol adapters in parallel and deduplicate."""
        discovery = get_device_family_discovery_service()
        families = discovery.get_all_families()

        if not families:
            logger.info("No device families available for device list")
            return []

        protocols = list(families.values())
        scan_tasks = [p.discover(timeout=SCAN_TIMEOUT) for p in protocols]
        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        all_discovered: list[DiscoveredDevice] = []
        for i, result in enumerate(results):
            protocol_name = protocols[i].protocol_name
            if isinstance(result, BaseException):
                logger.error("Protocol discover failed", protocol=protocol_name, error=str(result))
                continue
            discovered: list[DiscoveredDevice] = result
            logger.info("Protocol discover complete", protocol=protocol_name, device_count=len(discovered))
            all_discovered.extend(discovered)

        # Deduplicate: mac > ip > cloud_id > entity_id (same as device_scan_handler)
        by_key: dict[str, DiscoveredDevice] = {}
        for dev in all_discovered:
            if dev.mac_address:
                by_key[dev.mac_address.lower()] = dev
            elif dev.local_ip:
                by_key[dev.local_ip] = dev
            elif dev.cloud_id:
                by_key[dev.cloud_id] = dev
            else:
                by_key[dev.entity_id] = dev

        unique = list(by_key.values())
        logger.info("Jarvis Direct device list complete", device_count=len(unique))

        return [_to_manager_device(d) for d in unique]


def _to_manager_device(dev: DiscoveredDevice) -> DeviceManagerDevice:
    """Convert a DiscoveredDevice to a DeviceManagerDevice."""
    return DeviceManagerDevice(
        name=dev.name,
        domain=dev.domain,
        entity_id=dev.entity_id,
        is_controllable=dev.is_controllable,
        manufacturer=dev.manufacturer,
        model=dev.model,
        protocol=dev.protocol,
        local_ip=dev.local_ip,
        mac_address=dev.mac_address,
        cloud_id=dev.cloud_id,
        device_class=dev.device_class,
        source="direct",
    )
