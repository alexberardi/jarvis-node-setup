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
    room_name: str = ""


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
        self._http: httpx.AsyncClient | None = None
        self._load_protocols()

    def _load_protocols(self) -> None:
        """Load available protocol adapters via discovery service."""
        discovery = get_device_family_discovery_service()
        self._protocols = dict(discovery.get_all_families())

    def _http_client(self) -> httpx.AsyncClient:
        """Return a reused HTTP client for the life of this (singleton) service.

        Creating a fresh ``httpx.AsyncClient`` per call leaks native memory
        (connection pool + buffers, and an SSL context for HTTPS) even when the
        client is properly closed — measured at ~250 KB per HTTP cycle on the
        Pi. Because ``refresh_from_cc`` runs on a 5-minute background-agent
        schedule, that churn accumulates as GC-invisible native memory that
        ``malloc_trim`` can't reclaim, until the wake-detection path gets
        swapped and wake latency spikes. Reuse one client instead.
        """
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(timeout=10)
        return self._http

    async def aclose(self) -> None:
        """Close the reused HTTP client (call on node shutdown)."""
        if self._http is not None and not self._http.is_closed:
            await self._http.aclose()
        self._http = None

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
            client = self._http_client()
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
                    room_name=dev.get("room_name", ""),
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
            # Protocol may have been installed at runtime (Pantry) — reload once
            self._load_protocols()
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

    def invalidate_cache(self) -> None:
        """Drop the cached device registry.

        Call after CC reports a device add/remove so the next list/get
        triggers a fresh ``refresh_from_cc()`` instead of serving stale
        entries. Cheap — the next caller pays one HTTP round-trip.
        """
        self._device_cache = {}

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
            ip=device.local_ip or "",
            device=device,
            mac_address=device.mac_address,
        )

    async def remove_device(self, details: dict[str, Any]) -> None:
        """Let a device's protocol clean up when it's deleted from Jarvis (e.g.
        HomeKit unpair). CC publishes ``device_removed`` before deleting the
        record. Best-effort; always invalidates the cache afterward.
        """
        entity_id: str = details.get("entity_id", "")
        protocol: str = details.get("protocol", "")
        adapter = self._protocols.get(protocol)
        if adapter is None:
            self._load_protocols()
            adapter = self._protocols.get(protocol)
        try:
            if adapter is not None and hasattr(adapter, "on_removed"):
                from device_families.base import DiscoveredDevice

                discovered = DiscoveredDevice(
                    entity_id=entity_id,
                    name=details.get("name", ""),
                    domain=details.get("domain", ""),
                    manufacturer=protocol,
                    model=details.get("model", ""),
                    protocol=protocol,
                    cloud_id=details.get("cloud_id"),
                    local_ip=details.get("local_ip"),
                    mac_address=details.get("mac_address"),
                )
                await adapter.on_removed(discovered, entity_id=entity_id)
                logger.info("Protocol cleanup on device removal", entity_id=entity_id, protocol=protocol)
        except Exception as e:
            logger.warning("Device on_removed failed", entity_id=entity_id, error=str(e))
        finally:
            self.invalidate_cache()

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


# ---------------------------------------------------------------------------
# Process-wide singleton
#
# Pre-fix: DeviceDiscoveryAgent and ControlDeviceCommand each instantiated
# their own DirectDeviceService. The agent's 5-min refresh updated only
# its own cache; the command's cache was populated once on first use and
# never refreshed again. Result: a device added to CC after the command's
# cold start was invisible to control_device until process restart.
#
# All callers should now go through ``get_direct_device_service()`` so the
# agent's periodic refresh keeps the command path fresh too.
# ---------------------------------------------------------------------------

_direct_device_service_singleton: DirectDeviceService | None = None


def get_direct_device_service() -> DirectDeviceService:
    """Return the process-wide DirectDeviceService, creating it on first call."""
    global _direct_device_service_singleton
    if _direct_device_service_singleton is None:
        # Local imports keep this module importable without a Config — tests
        # that construct DirectDeviceService directly don't need it loaded.
        from utils.config_service import Config
        from utils.service_discovery import get_command_center_url

        _direct_device_service_singleton = DirectDeviceService(
            cc_base_url=get_command_center_url() or "",
            node_id=Config.get_str("node_id", "") or "",
            api_key=Config.get_str("api_key", "") or "",
            household_id=Config.get_str("household_id", "") or "",
        )
    return _direct_device_service_singleton


def reset_direct_device_service() -> None:
    """Drop the singleton (test hook). Production code should not call this."""
    global _direct_device_service_singleton
    _direct_device_service_singleton = None
