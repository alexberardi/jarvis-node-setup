"""Direct device service: controls WiFi smart devices without Home Assistant.

Maintains a registry of known devices (populated by DeviceScannerService)
and routes control commands to the appropriate protocol adapter.

Usage:
    service = DirectDeviceService()
    result = await service.control_device("light.bedroom_lifx", "turn_on")
    state = await service.get_state("light.bedroom_lifx")
"""

from dataclasses import dataclass
from typing import Any

import httpx
from jarvis_log_client import JarvisLogger

from device_families.base import DeviceControlResult, IJarvisDeviceProtocol
from utils.device_family_discovery_service import get_device_family_discovery_service

logger = JarvisLogger(service="jarvis-node")


@dataclass
class DeviceRecord:
    """Cached device info for routing control commands."""

    entity_id: str
    protocol: str
    local_ip: str
    mac_address: str
    domain: str
    name: str
    cloud_id: str = ""
    model: str = ""


class DirectDeviceService:
    """Routes device control to the correct protocol adapter.

    Fetches the device registry from command center on first use,
    then caches it. Resolves entity_id → protocol + IP and delegates
    to the matching adapter.
    """

    def __init__(
        self,
        cc_base_url: str = "",
        node_id: str = "",
        api_key: str = "",
        household_id: str = "",
    ) -> None:
        self._cc_base_url = cc_base_url.rstrip("/") if cc_base_url else ""
        self._node_id = node_id
        self._api_key = api_key
        self._household_id = household_id
        self._device_cache: dict[str, DeviceRecord] = {}
        self._protocols: dict[str, IJarvisDeviceProtocol] = {}
        self._load_protocols()

    def _load_protocols(self) -> None:
        """Load available protocol adapters via discovery service."""
        discovery = get_device_family_discovery_service()
        self._protocols = dict(discovery.get_all_families())

    def register_device(self, record: DeviceRecord) -> None:
        """Add or update a device in the local cache."""
        self._device_cache[record.entity_id] = record

    async def refresh_from_cc(self) -> int:
        """Fetch direct devices from command center and populate cache.

        Returns:
            Number of direct devices loaded.
        """
        if not self._cc_base_url:
            return 0

        url = f"{self._cc_base_url}/api/v0/node/devices"
        headers = {"X-API-Key": f"{self._node_id}:{self._api_key}"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
                resp.raise_for_status()
                devices = resp.json()

            count = 0
            for dev in devices:
                if dev.get("source") != "direct":
                    continue
                record = DeviceRecord(
                    entity_id=dev["entity_id"],
                    protocol=dev.get("protocol", ""),
                    local_ip=dev.get("local_ip", ""),
                    mac_address=dev.get("mac_address", ""),
                    domain=dev["domain"],
                    name=dev["name"],
                    cloud_id=dev.get("cloud_id", ""),
                    model=dev.get("model", ""),
                )
                self._device_cache[record.entity_id] = record
                count += 1

            logger.info("Direct device cache refreshed", device_count=count)
            return count

        except Exception as e:
            logger.error("Failed to refresh direct devices from CC", error=str(e))
            return 0

    def get_device(self, entity_id: str) -> DeviceRecord | None:
        """Look up a device by entity_id."""
        return self._device_cache.get(entity_id)

    def is_direct_device(self, entity_id: str) -> bool:
        """Check if entity_id belongs to a directly-controlled device."""
        return entity_id in self._device_cache

    def list_devices(self) -> list[DeviceRecord]:
        """List all cached direct devices."""
        return list(self._device_cache.values())

    async def control_device(
        self,
        entity_id: str,
        action: str,
        data: dict[str, Any] | None = None,
    ) -> DeviceControlResult:
        """Control a direct device by entity_id.

        Args:
            entity_id: Device entity ID (e.g., "light.living_room_lifx").
            action: Action name (e.g., "turn_on", "turn_off").
            data: Optional action-specific data.

        Returns:
            Result of the control operation.
        """
        device = self._device_cache.get(entity_id)
        if not device:
            # Try refreshing cache once
            await self.refresh_from_cc()
            device = self._device_cache.get(entity_id)

        if not device:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=f"Device not found: {entity_id}",
            )

        adapter = self._protocols.get(device.protocol)
        if not adapter:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=f"No adapter for protocol: {device.protocol}",
            )

        logger.info(
            "Direct device control",
            entity_id=entity_id, protocol=device.protocol,
            ip=device.local_ip, action=action,
        )

        from device_families.base import DiscoveredDevice

        discovered = DiscoveredDevice(
            entity_id=entity_id,
            name=device.name,
            domain=device.domain,
            manufacturer=device.protocol,
            model=device.model,
            protocol=device.protocol,
            cloud_id=device.cloud_id,
            local_ip=device.local_ip,
            mac_address=device.mac_address,
        )
        return await adapter.control(discovered, action, data or {})

    async def get_state(self, entity_id: str) -> dict[str, Any] | None:
        """Query current state of a direct device.

        Args:
            entity_id: Device entity ID.

        Returns:
            State dict or None on failure.
        """
        device = self._device_cache.get(entity_id)
        if not device:
            return None

        adapter = self._protocols.get(device.protocol)
        if not adapter:
            return None

        return await adapter.get_state(
            ip=device.local_ip,
            mac_address=device.mac_address,
        )

    def get_context_data(self) -> dict[str, Any]:
        """Build context data for voice prompts (same shape as HA context).

        Returns dict with "device_controls" keyed by domain, each containing
        a list of device dicts with entity_id, name, area, state.
        """
        device_controls: dict[str, list[dict[str, str]]] = {}
        for record in self._device_cache.values():
            if record.domain not in device_controls:
                device_controls[record.domain] = []
            device_controls[record.domain].append({
                "entity_id": record.entity_id,
                "name": record.name,
                "source": "direct",
                "protocol": record.protocol,
            })
        return {"device_controls": device_controls}
