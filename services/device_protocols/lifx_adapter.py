"""LIFX LAN protocol adapter.

Uses the lifxlan library for zero-cloud LIFX bulb/strip discovery and control.
LIFX devices broadcast on UDP 56700 and respond to LAN protocol messages.

Install: pip install lifxlan
"""

import asyncio
import re
from typing import Any

from jarvis_log_client import JarvisLogger

from services.device_protocols.base import (
    DeviceControlResult,
    DeviceProtocol,
    DiscoveredDevice,
)

logger = JarvisLogger(service="jarvis-node")


def _mac_to_str(mac_bytes: int | str) -> str:
    """Convert LIFX MAC (int or hex string) to standard AA:BB:CC:DD:EE:FF format."""
    if isinstance(mac_bytes, int):
        mac_hex = f"{mac_bytes:012x}"
    else:
        mac_hex = re.sub(r"[^0-9a-fA-F]", "", str(mac_bytes)).lower()
    return ":".join(mac_hex[i:i + 2] for i in range(0, 12, 2))


def _slugify(name: str) -> str:
    """Convert device name to HA-style entity slug."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class LifxProtocol(DeviceProtocol):
    """LIFX LAN protocol: discovery + control over UDP."""

    @property
    def protocol_name(self) -> str:
        return "lifx"

    @property
    def supported_domains(self) -> list[str]:
        return ["light"]

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        """Discover LIFX devices via LAN broadcast."""
        try:
            from lifxlan import LifxLAN
        except ImportError:
            logger.warning("lifxlan not installed, skipping LIFX discovery")
            return []

        def _scan() -> list[DiscoveredDevice]:
            lan = LifxLAN()
            devices = lan.get_lights(timeout=timeout)
            results: list[DiscoveredDevice] = []
            for dev in devices:
                try:
                    label = dev.get_label() or "LIFX Light"
                    ip = dev.get_ip_addr()
                    mac = _mac_to_str(dev.get_mac_addr())
                    product = dev.get_product() if hasattr(dev, "get_product") else None
                    model_name = f"LIFX {product}" if product else "LIFX"

                    slug = _slugify(label)
                    entity_id = f"light.{slug}"

                    results.append(DiscoveredDevice(
                        name=label,
                        domain="light",
                        manufacturer="LIFX",
                        model=model_name,
                        protocol="lifx",
                        local_ip=ip,
                        mac_address=mac,
                        entity_id=entity_id,
                        extra={"product": product},
                    ))
                except Exception as e:
                    logger.warning("Failed to query LIFX device", error=str(e))
            return results

        return await asyncio.to_thread(_scan)

    async def control(
        self,
        ip: str,
        action: str,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DeviceControlResult:
        """Control a LIFX device by IP."""
        try:
            from lifxlan import Light
        except ImportError:
            return DeviceControlResult(
                success=False, entity_id=kwargs.get("entity_id", ""), action=action,
                error="lifxlan not installed",
            )

        entity_id = kwargs.get("entity_id", f"light.{ip}")
        mac = kwargs.get("mac_address", "")

        def _control() -> DeviceControlResult:
            try:
                device = Light(mac, ip)

                if action == "turn_on":
                    brightness = 65535  # full
                    if data and "brightness" in data:
                        # Normalize 0-100 → 0-65535
                        brightness = int(float(data["brightness"]) / 100 * 65535)
                    device.set_power("on")
                    if data and "brightness" in data:
                        device.set_brightness(brightness)
                elif action == "turn_off":
                    device.set_power("off")
                elif action == "toggle":
                    power = device.get_power()
                    device.set_power("off" if power > 0 else "on")
                else:
                    return DeviceControlResult(
                        success=False, entity_id=entity_id, action=action,
                        error=f"Unsupported action: {action}",
                    )

                return DeviceControlResult(success=True, entity_id=entity_id, action=action)
            except Exception as e:
                return DeviceControlResult(
                    success=False, entity_id=entity_id, action=action, error=str(e),
                )

        return await asyncio.to_thread(_control)

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        """Query LIFX device state."""
        try:
            from lifxlan import Light
        except ImportError:
            return None

        mac = kwargs.get("mac_address", "")

        def _query() -> dict[str, Any] | None:
            try:
                device = Light(mac, ip)
                power = device.get_power()
                color = device.get_color()  # (hue, saturation, brightness, kelvin)
                return {
                    "state": "on" if power > 0 else "off",
                    "brightness": round(color[2] / 65535 * 100) if color else None,
                    "color_temp": color[3] if color else None,
                }
            except Exception:
                return None

        return await asyncio.to_thread(_query)
