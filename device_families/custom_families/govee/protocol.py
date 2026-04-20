"""Govee smart device protocol adapter (hybrid LAN+cloud)."""

from __future__ import annotations

import re
from typing import Any

from jarvis_command_sdk import (
    IJarvisDeviceProtocol,
    IJarvisSecret,
    DiscoveredDevice,
    DeviceControlResult,
    IJarvisButton,
    JarvisSecret,
    JarvisStorage,
)

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw: Any) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: Any) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: Any) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: Any) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: Any) -> None:
            self._log.debug(msg)


logger = JarvisLogger(service="device.govee")

_storage = JarvisStorage("govee")

GOVEE_API_BASE: str = "https://openapi.api.govee.com/router/api/v1"
GOVEE_API_LEGACY_BASE: str = "https://developer-api.govee.com/v1"

_GOVEE_TYPE_TO_DOMAIN: dict[str, str] = {
    "light": "light",
    "led": "light",
    "bulb": "light",
    "lamp": "light",
    "strip": "light",
    "plug": "switch",
    "socket": "switch",
    "switch": "switch",
    "outlet": "switch",
    "kettle": "kettle",
    "heater": "switch",
    "humidifier": "switch",
    "purifier": "switch",
    "fan": "switch",
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _classify_device(device_name: str, model: str) -> str:
    combined: str = f"{device_name} {model}".lower()
    for keyword, domain in _GOVEE_TYPE_TO_DOMAIN.items():
        if keyword in combined:
            return domain
    return "light"


class GoveeProtocol(IJarvisDeviceProtocol):
    """Govee hybrid LAN+cloud protocol adapter."""

    protocol_name: str = "govee"
    friendly_name: str = "Govee"
    supported_domains: list[str] = ["switch", "light", "kettle"]
    connection_type: str = "hybrid"
    setup_guide: str = """## Getting Your Govee API Key

1. Open the **Govee Home** app on your phone
2. Tap your **profile icon** (bottom right)
3. Go to **Settings** (gear icon)
4. Tap **About Us** → **Apply for API Key**
5. Fill out the application form
6. Govee will email your API key (usually within minutes)
7. Paste the key here

The API key gives access to all Govee devices on your account."""

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "GOVEE_API_KEY",
                "Govee Developer API key (https://developer.govee.com)",
                "integration", "string",
                friendly_name="API Key",
            ),
        ]

    def _get_api_key(self) -> str | None:
        return _storage.get_secret("GOVEE_API_KEY")

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(
                button_text="Turn On",
                button_action="turn_on",
                button_type="primary",
                button_icon="power-plug",
            ),
            IJarvisButton(
                button_text="Turn Off",
                button_action="turn_off",
                button_type="secondary",
                button_icon="power-plug-off",
            ),
        ]

    async def discover(self, timeout: int = 5) -> list[DiscoveredDevice]:
        api_key: str | None = self._get_api_key()
        if not api_key:
            logger.error("GOVEE_API_KEY not configured")
            return []

        try:
            import httpx
        except ImportError:
            logger.error("httpx is not installed. Run: pip install httpx")
            return []

        devices: list[DiscoveredDevice] = []
        headers: dict[str, str] = {"Govee-API-Key": api_key}

        # Try current API first
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{GOVEE_API_BASE}/user/devices",
                    headers=headers,
                )
                if resp.status_code == 200:
                    data: dict[str, Any] = resp.json()
                    raw_devices: list[dict[str, Any]] = data.get("data", [])
                    for dev in raw_devices:
                        sku: str = dev.get("sku", "")
                        device_id_raw: str = dev.get("device", "")
                        device_name: str = dev.get("deviceName", "") or sku
                        domain: str = _classify_device(device_name, sku)

                        devices.append(
                            DiscoveredDevice(
                                entity_id=_slugify(device_name) if device_name else _slugify(device_id_raw),
                                name=device_name or "Govee Device",
                                domain=domain,
                                protocol=self.protocol_name,
                                model=sku,
                                manufacturer="Govee",
                                cloud_id=device_id_raw,
                                extra={"sku": sku},
                            )
                        )

                    logger.info(f"Govee discovery found {len(devices)} device(s) via current API")
                    return devices
        except Exception as e:
            logger.warning(f"Govee current API failed, trying legacy: {e}")

        # Fall back to legacy v1 API
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{GOVEE_API_LEGACY_BASE}/devices",
                    headers=headers,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    raw_devices = data.get("data", {}).get("devices", [])
                    for dev in raw_devices:
                        model: str = dev.get("model", "")
                        device_id_raw = dev.get("device", "")
                        device_name = dev.get("deviceName", "") or model
                        domain = _classify_device(device_name, model)

                        devices.append(
                            DiscoveredDevice(
                                entity_id=_slugify(device_name) if device_name else _slugify(device_id_raw),
                                name=device_name or "Govee Device",
                                domain=domain,
                                protocol=self.protocol_name,
                                model=model,
                                manufacturer="Govee",
                                cloud_id=device_id_raw,
                                extra={"model": model},
                            )
                        )

                    logger.info(f"Govee discovery found {len(devices)} device(s) via legacy API")
                else:
                    logger.error(f"Govee legacy API returned {resp.status_code}")
        except Exception as e:
            logger.error(f"Govee legacy API failed: {e}")

        return devices

    async def control(
        self, device: DiscoveredDevice, action: str, params: dict[str, Any] | None = None
    ) -> DeviceControlResult:
        api_key: str | None = self._get_api_key()
        if not api_key:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error="GOVEE_API_KEY not configured")

        try:
            import httpx
        except ImportError:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error="httpx is not installed. Run: pip install httpx",
            )

        params = params or {}
        cloud_id: str = device.cloud_id or ""
        sku: str = device.model or ""
        if not cloud_id:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error="No cloud device ID available")

        headers: dict[str, str] = {
            "Govee-API-Key": api_key,
            "Content-Type": "application/json",
        }

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
        elif action == "toggle":
            state: dict[str, Any] = await self.get_state(device)
            is_on: bool = state.get("state") == "on"
            capability = {
                "type": "devices.capabilities.on_off",
                "instance": "powerSwitch",
                "value": 0 if is_on else 1,
            }
        elif action == "set_brightness":
            brightness: int = int(params.get("brightness", 100))
            brightness = max(0, min(100, brightness))
            capability = {
                "type": "devices.capabilities.range",
                "instance": "brightness",
                "value": brightness,
            }
        elif action == "set_temperature":
            temp: int = int(params.get("temperature", 100))
            capability = {
                "type": "devices.capabilities.range",
                "instance": "temperature",
                "value": temp,
            }
        elif action == "set_mode":
            mode: str = str(params.get("mode", "boil"))
            capability = {
                "type": "devices.capabilities.mode",
                "instance": "workMode",
                "value": mode,
            }
        elif action == "set_color":
            if "rgb" in params:
                rgb = params["rgb"]
                r, g, b = int(rgb[0]), int(rgb[1]), int(rgb[2])
                capability = {
                    "type": "devices.capabilities.color_setting",
                    "instance": "colorRgb",
                    "value": (r << 16) + (g << 8) + b,
                }
            elif "color_temp" in params:
                color_temp: int = int(params["color_temp"])
                capability = {
                    "type": "devices.capabilities.color_setting",
                    "instance": "colorTemperatureK",
                    "value": color_temp,
                }
            else:
                return DeviceControlResult(
                    success=False, entity_id=device.entity_id, action=action,
                    error="set_color requires 'rgb' or 'color_temp' param",
                )
        else:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Unsupported action: {action}")

        payload: dict[str, Any] = {
            "requestId": "jarvis",
            "payload": {
                "sku": sku,
                "device": cloud_id,
                "capability": capability,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{GOVEE_API_BASE}/device/control",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    return DeviceControlResult(
                        success=True, entity_id=device.entity_id, action=action,
                    )
                else:
                    body: str = resp.text
                    return DeviceControlResult(
                        success=False, entity_id=device.entity_id, action=action,
                        error=f"Govee API returned {resp.status_code}: {body}",
                    )
        except Exception as e:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Control failed: {e}")

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        api_key: str | None = self._get_api_key()
        if not api_key:
            return {"error": "GOVEE_API_KEY not configured"}

        try:
            import httpx
        except ImportError:
            return {"error": "httpx is not installed"}

        # Accept cloud_id/model from kwargs (device_state_handler passes
        # these directly) or from a DiscoveredDevice if the caller sends one.
        device: DiscoveredDevice | None = kwargs.get("device")
        cloud_id: str = kwargs.get("cloud_id") or (device.cloud_id if device else "") or ""
        sku: str = kwargs.get("model") or (device.model if device else "") or ""
        if not cloud_id:
            return {"error": "No cloud device ID available"}

        headers: dict[str, str] = {
            "Govee-API-Key": api_key,
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {
            "requestId": "jarvis",
            "payload": {
                "sku": sku,
                "device": cloud_id,
            },
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{GOVEE_API_BASE}/device/state",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 200:
                    data: dict[str, Any] = resp.json()
                    capabilities: list[dict[str, Any]] = (
                        data.get("payload", {}).get("capabilities", [])
                    )

                    state: dict[str, Any] = {}
                    for cap in capabilities:
                        instance: str = cap.get("instance", "")
                        value: Any = cap.get("state", {}).get("value")
                        if instance == "powerSwitch":
                            state["state"] = "on" if value == 1 else "off"
                        elif instance == "brightness":
                            state["brightness"] = value
                        elif instance == "colorRgb":
                            if isinstance(value, int):
                                state["rgb"] = [
                                    (value >> 16) & 0xFF,
                                    (value >> 8) & 0xFF,
                                    value & 0xFF,
                                ]
                        elif instance == "colorTemperatureK":
                            state["color_temp"] = value

                    return state
                else:
                    return {"error": f"Govee API returned {resp.status_code}"}
        except Exception as e:
            return {"error": f"Failed to get state: {e}"}
