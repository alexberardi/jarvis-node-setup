"""Govee device family adapter (hybrid: LAN UDP + cloud REST).

Govee devices support both LAN control (UDP multicast on port 4001/4003)
and cloud control via the Govee API. The cloud API requires an API key
from https://developer.govee.com.

LAN discovery works without an API key. Cloud control/state queries
require GOVEE_API_KEY.

Cloud API docs: https://developer.govee.com/reference/get-you-devices
"""

import re
from typing import Any, Literal

import httpx
from jarvis_log_client import JarvisLogger

from core.ijarvis_button import IJarvisButton
from core.ijarvis_secret import JarvisSecret
from device_families.base import (
    DeviceControlResult,
    IJarvisDeviceProtocol,
    DiscoveredDevice,
)

logger = JarvisLogger(service="jarvis-node")

# Govee has two API versions:
# - Legacy v1: https://developer-api.govee.com/v1/devices (older lights/plugs only)
# - Current:   https://openapi.api.govee.com/router/api/v1/user/devices (all devices)
GOVEE_API_BASE = "https://openapi.api.govee.com"
GOVEE_API_LEGACY_BASE = "https://developer-api.govee.com"

# Map Govee capability types to HA domains
_GOVEE_TYPE_TO_DOMAIN: dict[str, str] = {
    "devices.types.light": "light",
    "devices.types.air_purifier": "fan",
    "devices.types.humidifier": "fan",
    "devices.types.thermometer": "sensor",
    "devices.types.socket": "switch",
    "devices.types.heater": "climate",
    "devices.types.sensor": "sensor",
    "devices.types.aroma_diffuser": "switch",
    "devices.types.ice_maker": "switch",
    "devices.types.kettle": "kettle",
}


def _slugify(name: str) -> str:
    """Convert device name to HA-style entity slug."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _get_api_key() -> str | None:
    """Load the GOVEE_API_KEY from the secrets DB."""
    from services.secret_service import get_secret_value

    return get_secret_value("GOVEE_API_KEY", "integration")


class GoveeProtocol(IJarvisDeviceProtocol):
    """Govee hybrid protocol: LAN UDP discovery + cloud REST control."""

    @property
    def protocol_name(self) -> str:
        return "govee"

    @property
    def friendly_name(self) -> str:
        return "Govee"

    @property
    def description(self) -> str:
        return "Govee smart devices (LAN + cloud control)"

    @property
    def supported_domains(self) -> list[str]:
        return ["switch", "light", "kettle"]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton("Turn On", "turn_on", "primary", "power"),
            IJarvisButton("Turn Off", "turn_off", "secondary", "power-off"),
        ]

    @property
    def connection_type(self) -> Literal["lan", "cloud", "hybrid"]:
        return "hybrid"

    @property
    def required_secrets(self) -> list[JarvisSecret]:
        return [
            JarvisSecret(
                key="GOVEE_API_KEY",
                description="Govee Developer API key (https://developer.govee.com)",
                scope="integration",
                value_type="string",
                required=True,
                is_sensitive=True,
                friendly_name="Govee API Key",
            ),
        ]

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        """Discover Govee devices via the cloud API.

        Tries the current API first (openapi.api.govee.com), then falls back
        to the legacy v1 API (developer-api.govee.com) for older accounts.
        """
        api_key = _get_api_key()
        if not api_key:
            logger.debug("GOVEE_API_KEY not set, skipping Govee discovery")
            return []

        # Try the current API first
        results = await self._discover_current_api(api_key, timeout)
        if results is not None:
            return results

        # Fall back to legacy v1 API
        return await self._discover_legacy_api(api_key, timeout)

    async def _discover_current_api(
        self, api_key: str, timeout: float
    ) -> list[DiscoveredDevice] | None:
        """Discover via the current Govee API (supports all devices including appliances).

        Returns None if the API call fails (to trigger legacy fallback).
        """
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{GOVEE_API_BASE}/router/api/v1/user/devices",
                    headers={
                        "Govee-API-Key": api_key,
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            logger.warning(
                "Govee current API failed, will try legacy",
                status=e.response.status_code,
                body=e.response.text[:200],
            )
            return None
        except Exception as e:
            logger.warning("Govee current API failed, will try legacy", error=str(e))
            return None

        devices_data: list[dict[str, Any]] = body.get("data", [])
        results: list[DiscoveredDevice] = []

        for dev in devices_data:
            sku: str = dev.get("sku", "Unknown")
            device_id: str = dev.get("device", "")
            device_name: str = dev.get("deviceName", "") or f"Govee {sku}"
            capabilities: list[dict[str, Any]] = dev.get("capabilities", [])
            device_type: str = dev.get("type", "")

            # Determine domain from type field or capabilities
            domain = _GOVEE_TYPE_TO_DOMAIN.get(device_type, "switch")
            if sku.startswith(("H61", "H60", "H70")):
                domain = "light"

            # Check if device is controllable (has on_off or toggle capability)
            cap_types = [c.get("type", "") for c in capabilities]
            controllable = any(
                "on_off" in t or "toggle" in t or "brightness" in t
                for t in cap_types
            )

            slug = _slugify(device_name)
            entity_id = f"{domain}.{slug}"

            results.append(DiscoveredDevice(
                name=device_name,
                domain=domain,
                manufacturer="Govee",
                model=sku,
                protocol="govee",
                entity_id=entity_id,
                cloud_id=device_id,
                is_controllable=controllable,
                extra={
                    "device_type": device_type,
                    "capabilities": cap_types,
                    "api_version": "current",
                },
            ))

        logger.info("Govee discovery complete (current API)", device_count=len(results))
        return results

    async def _discover_legacy_api(
        self, api_key: str, timeout: float
    ) -> list[DiscoveredDevice]:
        """Discover via the legacy v1 API (older lights/plugs only)."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{GOVEE_API_LEGACY_BASE}/v1/devices",
                    headers={"Govee-API-Key": api_key},
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            logger.error("Govee legacy API error", status=e.response.status_code, body=e.response.text[:200])
            return []
        except Exception as e:
            logger.error("Govee legacy API discovery failed", error=str(e))
            return []

        devices_data: list[dict[str, Any]] = body.get("data", {}).get("devices", [])
        results: list[DiscoveredDevice] = []

        for dev in devices_data:
            device_name = dev.get("deviceName", "Govee Device")
            model = dev.get("model", "Unknown")
            device_id = dev.get("device", "")
            controllable = dev.get("controllable", False)
            device_type = dev.get("type", "")

            domain = _GOVEE_TYPE_TO_DOMAIN.get(device_type, "switch")
            if model.startswith(("H61", "H60", "H70")):
                domain = "light"

            slug = _slugify(device_name)
            entity_id = f"{domain}.{slug}"

            results.append(DiscoveredDevice(
                name=device_name,
                domain=domain,
                manufacturer="Govee",
                model=model,
                protocol="govee",
                entity_id=entity_id,
                cloud_id=device_id,
                is_controllable=controllable,
                extra={
                    "device_type": device_type,
                    "supported_commands": dev.get("supportCmds", []),
                    "api_version": "legacy_v1",
                },
            ))

        logger.info("Govee discovery complete (legacy API)", device_count=len(results))
        return results

    async def control(
        self,
        ip: str,
        action: str,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DeviceControlResult:
        """Control a Govee device via the cloud API.

        Args:
            ip: Unused for cloud control.
            action: "turn_on", "turn_off", or "set_brightness".
            data: Optional {"brightness": 0-100} for brightness control.
            **kwargs: Must include "cloud_id" (device MAC) and "model".
        """
        api_key = _get_api_key()
        entity_id = kwargs.get("entity_id", "")
        cloud_id = kwargs.get("cloud_id", "")
        model = kwargs.get("model", "")

        if not api_key:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error="GOVEE_API_KEY not configured",
            )

        if not cloud_id or not model:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error="cloud_id and model are required for Govee cloud control",
            )

        # Build the capability payload (current API format)
        capability: dict[str, Any] = {}
        if action == "turn_on":
            capability = {
                "type": "devices.capabilities.on_off",
                "instance": "powerSwitch",
                "value": 1,
            }
        elif action == "turn_off":
            capability = {
                "type": "devices.capabilities.on_off",
                "instance": "powerSwitch",
                "value": 0,
            }
        elif action == "set_brightness" and data and "brightness" in data:
            capability = {
                "type": "devices.capabilities.range",
                "instance": "brightness",
                "value": int(data["brightness"]),
            }
        elif action == "toggle":
            state = await self.get_state(ip, cloud_id=cloud_id, model=model)
            value = 0 if state and state.get("state") == "on" else 1
            capability = {
                "type": "devices.capabilities.on_off",
                "instance": "powerSwitch",
                "value": value,
            }
        elif action == "set_temperature" and data and "temperature" in data:
            # Kettle temperature control (40-100°C)
            temp = int(float(data["temperature"]))
            unit = data.get("unit", "Celsius")
            capability = {
                "type": "devices.capabilities.temperature_setting",
                "instance": "sliderTemperature",
                "value": {
                    "targetTemperature": temp,
                    "unit": unit,
                },
            }
        elif action == "set_mode" and data and "mode" in data:
            # Kettle work mode (boil, keep_warm, etc.)
            mode_value = data["mode"]
            capability = {
                "type": "devices.capabilities.work_mode",
                "instance": "workMode",
                "value": mode_value,
            }
        elif action == "set_color" and data:
            if "rgb" in data:
                r, g, b = data["rgb"]
                capability = {
                    "type": "devices.capabilities.color_setting",
                    "instance": "colorRgb",
                    "value": (int(r) << 16) | (int(g) << 8) | int(b),
                }
            elif "color_temp" in data:
                capability = {
                    "type": "devices.capabilities.color_setting",
                    "instance": "colorTemperatureK",
                    "value": int(data["color_temp"]),
                }
            else:
                return DeviceControlResult(
                    success=False, entity_id=entity_id, action=action,
                    error="Provide rgb or color_temp for set_color",
                )
        else:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=f"Unsupported action: {action}",
            )

        payload: dict[str, Any] = {
            "requestId": "jarvis-ctrl",
            "payload": {
                "sku": model,
                "device": cloud_id,
                "capability": capability,
            },
        }

        try:
            # Govee's API sends the command to the device immediately but
            # their HTTP response can take 5-10s. Use a short timeout —
            # if the POST was accepted, the device will act on it.
            async with httpx.AsyncClient(timeout=3) as client:
                resp = await client.post(
                    f"{GOVEE_API_BASE}/router/api/v1/device/control",
                    json=payload,
                    headers={
                        "Govee-API-Key": api_key,
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()

            return DeviceControlResult(success=True, entity_id=entity_id, action=action)

        except httpx.HTTPStatusError as e:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=f"Govee API {e.response.status_code}: {e.response.text[:100]}",
            )
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ConnectTimeout):
            # Timeout after sending — Govee received it, device will respond
            logger.debug("Govee API timeout after send (command likely delivered)")
            return DeviceControlResult(success=True, entity_id=entity_id, action=action)
        except Exception as e:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=str(e),
            )

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        """Query Govee device state via the cloud API."""
        api_key = _get_api_key()
        cloud_id = kwargs.get("cloud_id", "")
        model = kwargs.get("model", "")

        if not api_key or not cloud_id or not model:
            return None

        payload: dict[str, Any] = {
            "requestId": "jarvis-state",
            "payload": {
                "sku": model,
                "device": cloud_id,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{GOVEE_API_BASE}/router/api/v1/device/state",
                    json=payload,
                    headers={
                        "Govee-API-Key": api_key,
                        "Content-Type": "application/json",
                    },
                )
                resp.raise_for_status()
                body = resp.json()

            # Parse capabilities from response
            capabilities: list[dict[str, Any]] = (
                body.get("payload", {}).get("capabilities", [])
            )
            state: dict[str, Any] = {}
            for cap in capabilities:
                cap_type = cap.get("type", "")
                instance = cap.get("instance", "")
                value = cap.get("state", {}).get("value")

                if "on_off" in cap_type:
                    state["state"] = "on" if value == 1 else "off"
                elif instance == "brightness":
                    state["brightness"] = value
                elif instance == "colorRgb":
                    # Govee packs RGB as integer: (r << 16) | (g << 8) | b
                    if isinstance(value, int):
                        state["rgb"] = [
                            (value >> 16) & 0xFF,
                            (value >> 8) & 0xFF,
                            value & 0xFF,
                        ]
                    else:
                        state["color"] = value
                elif instance == "colorTemperatureK":
                    state["color_temp"] = value
                elif "online" in cap_type:
                    state["online"] = bool(value)
                elif "temperature_setting" in cap_type and instance == "sliderTemperature":
                    # Kettle target temperature: value is dict or int
                    if isinstance(value, dict):
                        state["target_temperature"] = value.get("targetTemperature")
                        state["unit"] = value.get("unit", "Celsius")
                    elif isinstance(value, (int, float)):
                        state["target_temperature"] = int(value)
                elif "sensor" in cap_type and "temperature" in instance:
                    # Kettle current water temperature
                    if isinstance(value, (int, float)):
                        state["current_temperature"] = int(value)
                elif "work_mode" in cap_type:
                    state["mode"] = value

            return state if state else None

        except Exception as e:
            logger.warning("Govee state query failed", error=str(e), device=cloud_id)
            return None
