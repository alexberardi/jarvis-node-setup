"""TP-Link Kasa/Tapo LAN protocol adapter.

Uses the python-kasa library for zero-cloud discovery and control of
TP-Link smart plugs, bulbs, switches, strips, and dimmers.

Install: pip install python-kasa
"""

import re
from typing import Any

from jarvis_log_client import JarvisLogger

from core.ijarvis_button import IJarvisButton
from device_families.base import (
    DeviceControlResult,
    DeviceProtocol,
    DiscoveredDevice,
)

logger = JarvisLogger(service="jarvis-node")

# Map python-kasa device types to HA domains
_KASA_TYPE_TO_DOMAIN: dict[str, str] = {
    "plug": "switch",
    "bulb": "light",
    "strip": "switch",
    "dimmer": "light",
    "lightstrip": "light",
    "wallswitch": "switch",
    "fan": "fan",
}


def _slugify(name: str) -> str:
    """Convert device name to HA-style entity slug."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class KasaProtocol(DeviceProtocol):
    """TP-Link Kasa/Tapo LAN protocol: discovery + control."""

    @property
    def protocol_name(self) -> str:
        return "kasa"

    @property
    def friendly_name(self) -> str:
        return "TP-Link Kasa"

    @property
    def description(self) -> str:
        return "TP-Link Kasa/Tapo smart devices (LAN control)"

    @property
    def supported_domains(self) -> list[str]:
        return ["light", "switch", "fan"]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton("Turn On", "turn_on", "primary", "power"),
            IJarvisButton("Turn Off", "turn_off", "secondary", "power-off"),
            IJarvisButton("Toggle", "toggle", "secondary", "toggle-switch"),
        ]

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        """Discover Kasa/Tapo devices via broadcast."""
        try:
            from kasa import Discover
        except ImportError:
            logger.warning("python-kasa not installed, skipping Kasa discovery")
            return []

        try:
            found = await Discover.discover(timeout=int(timeout))
        except Exception as e:
            logger.error("Kasa discovery failed", error=str(e))
            return []

        results: list[DiscoveredDevice] = []
        for ip, dev in found.items():
            try:
                await dev.update()
                alias = dev.alias or f"Kasa {ip}"
                mac = dev.mac or ""
                model = dev.model or "Unknown"
                device_type = dev.device_type.name.lower() if hasattr(dev, "device_type") else "plug"
                domain = _KASA_TYPE_TO_DOMAIN.get(device_type, "switch")

                slug = _slugify(alias)
                entity_id = f"{domain}.{slug}"

                results.append(DiscoveredDevice(
                    name=alias,
                    domain=domain,
                    manufacturer="TP-Link",
                    model=model,
                    protocol="kasa",
                    local_ip=str(ip),
                    mac_address=mac,
                    entity_id=entity_id,
                    extra={"device_type": device_type},
                ))
            except Exception as e:
                logger.warning("Failed to query Kasa device", ip=str(ip), error=str(e))

        return results

    async def control(
        self,
        ip: str,
        action: str,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DeviceControlResult:
        """Control a Kasa device by IP."""
        try:
            from kasa import Discover
        except ImportError:
            return DeviceControlResult(
                success=False, entity_id=kwargs.get("entity_id", ""), action=action,
                error="python-kasa not installed",
            )

        entity_id = kwargs.get("entity_id", f"switch.{ip}")

        try:
            dev = await Discover.discover_single(ip, timeout=5)
            await dev.update()

            if action == "turn_on":
                await dev.turn_on()
                if data and "brightness" in data and hasattr(dev, "set_brightness"):
                    await dev.set_brightness(int(data["brightness"]))
            elif action == "turn_off":
                await dev.turn_off()
            elif action == "toggle":
                if dev.is_on:
                    await dev.turn_off()
                else:
                    await dev.turn_on()
            else:
                return DeviceControlResult(
                    success=False, entity_id=entity_id, action=action,
                    error=f"Unsupported action: {action}",
                )

            await dev.update()
            return DeviceControlResult(success=True, entity_id=entity_id, action=action)

        except Exception as e:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action, error=str(e),
            )

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        """Query Kasa device state."""
        try:
            from kasa import Discover
        except ImportError:
            return None

        try:
            dev = await Discover.discover_single(ip, timeout=5)
            await dev.update()
            state: dict[str, Any] = {
                "state": "on" if dev.is_on else "off",
            }
            if hasattr(dev, "brightness"):
                state["brightness"] = dev.brightness
            return state
        except Exception:
            return None
