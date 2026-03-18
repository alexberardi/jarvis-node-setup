"""Device scanner service: discovers WiFi smart devices on the LAN.

Runs periodic mDNS + protocol-specific scans, deduplicates by MAC address,
and reports discovered devices to the command center via the bulk import API.

Usage:
    scanner = DeviceScannerService(cc_base_url, node_id, api_key, household_id)
    await scanner.scan_and_report()  # one-shot
    await scanner.start_periodic(interval=300)  # every 5 min
"""

import asyncio
from typing import Any

import httpx
from jarvis_log_client import JarvisLogger

from device_families.base import IJarvisDeviceProtocol, DiscoveredDevice
from utils.device_family_discovery_service import get_device_family_discovery_service

logger = JarvisLogger(service="jarvis-node")


class DeviceScannerService:
    """Discovers LAN devices and registers them with the command center."""

    def __init__(
        self,
        cc_base_url: str,
        node_id: str,
        api_key: str,
        household_id: str,
        admin_key: str = "",
        scan_timeout: float = 5.0,
    ) -> None:
        self._cc_base_url = cc_base_url.rstrip("/")
        self._node_id = node_id
        self._api_key = api_key
        self._admin_key = admin_key
        self._household_id = household_id
        self._scan_timeout = scan_timeout
        discovery = get_device_family_discovery_service()
        self._protocols = list(discovery.get_all_families().values())
        self._known_macs: dict[str, DiscoveredDevice] = {}
        self._running = False

    @property
    def protocol_count(self) -> int:
        return len(self._protocols)

    async def scan(self) -> list[DiscoveredDevice]:
        """Run all protocol scanners and deduplicate by MAC address.

        Returns:
            List of unique discovered devices.
        """
        if not self._protocols:
            logger.warning("No device protocols available — install lifxlan or python-kasa")
            return []

        all_devices: list[DiscoveredDevice] = []
        scan_tasks = [p.discover(timeout=self._scan_timeout) for p in self._protocols]
        results = await asyncio.gather(*scan_tasks, return_exceptions=True)

        for i, result in enumerate(results):
            protocol_name = self._protocols[i].protocol_name
            if isinstance(result, Exception):
                logger.error("Protocol scan failed", protocol=protocol_name, error=str(result))
                continue
            logger.info("Protocol scan complete", protocol=protocol_name, device_count=len(result))
            all_devices.extend(result)

        # Deduplicate by MAC address (prefer latest scan data)
        by_mac: dict[str, DiscoveredDevice] = {}
        for dev in all_devices:
            if dev.mac_address:
                by_mac[dev.mac_address.lower()] = dev
            elif dev.local_ip:
                # No MAC — use IP as fallback key
                by_mac[dev.local_ip] = dev
            elif dev.cloud_id:
                # Cloud-only device — use cloud_id as key
                by_mac[dev.cloud_id] = dev
            else:
                # Last resort — use entity_id
                by_mac[dev.entity_id] = dev

        # Resolve entity_id collisions (e.g., two "light.living_room")
        unique = list(by_mac.values())
        unique = self._resolve_entity_collisions(unique)

        self._known_macs = by_mac
        return unique

    def _resolve_entity_collisions(self, devices: list[DiscoveredDevice]) -> list[DiscoveredDevice]:
        """Append protocol suffix to entity_id if duplicates exist."""
        seen: dict[str, int] = {}
        for dev in devices:
            if dev.entity_id in seen:
                seen[dev.entity_id] += 1
                dev.entity_id = f"{dev.entity_id}_{dev.protocol}"
            else:
                seen[dev.entity_id] = 1
        return devices

    async def report_to_cc(self, devices: list[DiscoveredDevice]) -> dict[str, Any]:
        """Send discovered devices to command center via bulk import API.

        Args:
            devices: Devices to register.

        Returns:
            CC response dict with created/updated counts.
        """
        if not devices:
            return {"created": 0, "updated": 0}

        import_items = []
        for dev in devices:
            item: dict[str, Any] = {
                "entity_id": dev.entity_id,
                "name": dev.name,
                "domain": dev.domain,
                "device_class": dev.device_class,
                "manufacturer": dev.manufacturer,
                "model": dev.model,
                "source": "direct",
                "protocol": dev.protocol,
            }
            if dev.local_ip:
                item["local_ip"] = dev.local_ip
            if dev.mac_address:
                item["mac_address"] = dev.mac_address
            if dev.cloud_id:
                item["cloud_id"] = dev.cloud_id
            import_items.append(item)

        url = f"{self._cc_base_url}/api/v0/households/{self._household_id}/devices/import"
        # CC smart home endpoints accept admin key or JWT
        auth_key = self._admin_key or self._api_key
        headers = {"X-API-Key": auth_key}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(url, json={"devices": import_items}, headers=headers)
                resp.raise_for_status()
                result = resp.json()
                logger.info(
                    "Devices reported to CC",
                    created=result.get("created", 0),
                    updated=result.get("updated", 0),
                    total=len(devices),
                )
                return result
        except httpx.HTTPStatusError as e:
            logger.error("CC device import failed", status=e.response.status_code, body=e.response.text[:200])
            return {"error": str(e)}
        except Exception as e:
            logger.error("CC device import failed", error=str(e))
            return {"error": str(e)}

    async def scan_and_report(self) -> dict[str, Any]:
        """One-shot: scan LAN and report to command center.

        Returns:
            Dict with scan results and CC response.
        """
        devices = await self.scan()
        if not devices:
            logger.info("No devices discovered on LAN")
            return {"discovered": 0}

        logger.info("Devices discovered on LAN", count=len(devices),
                     protocols=[d.protocol for d in devices])

        cc_result = await self.report_to_cc(devices)
        return {"discovered": len(devices), "cc_result": cc_result}

    async def start_periodic(self, interval: int = 300) -> None:
        """Start periodic scanning in the background.

        Args:
            interval: Seconds between scans (default: 5 minutes).
        """
        self._running = True
        logger.info("Starting periodic device scan", interval_s=interval)

        while self._running:
            try:
                await self.scan_and_report()
            except Exception as e:
                logger.error("Periodic scan failed", error=str(e))
            await asyncio.sleep(interval)

    def stop(self) -> None:
        """Stop periodic scanning."""
        self._running = False
