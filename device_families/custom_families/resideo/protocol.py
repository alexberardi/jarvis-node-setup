"""Resideo / Honeywell Home protocol adapter (Home API v2)."""

from __future__ import annotations

import re
from typing import Any

from jarvis_command_sdk import (
    IJarvisDeviceProtocol,
    DiscoveredDevice,
    DeviceControlResult,
    IJarvisButton,
    JarvisSecret,
    AuthenticationConfig,
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


logger = JarvisLogger(service="device.resideo")

_storage = JarvisStorage("resideo")

API_BASE: str = "https://api.honeywellhome.com/v2"


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


class ResideoProtocol(IJarvisDeviceProtocol):
    """Resideo / Honeywell Home thermostat control via Home API v2."""

    protocol_name: str = "resideo"
    friendly_name: str = "Resideo / Honeywell Home"
    supported_domains: list[str] = ["climate"]
    connection_type: str = "cloud"

    # ── credential helpers ──────────────────────────────────────────

    def _get_consumer_key(self) -> str | None:
        return _storage.get_secret("RESIDEO_CONSUMER_KEY")

    def _get_consumer_secret(self) -> str | None:
        return _storage.get_secret("RESIDEO_CONSUMER_SECRET")

    def _get_access_token(self) -> str | None:
        return _storage.get_secret("RESIDEO_ACCESS_TOKEN")

    def _get_temp_unit(self) -> str:
        unit: str | None = _storage.get_secret("RESIDEO_TEMP_UNIT")
        if unit and unit.upper() in ("C", "F"):
            return unit.upper()
        return "F"

    # ── SDK properties ──────────────────────────────────────────────

    @property
    def required_secrets(self) -> list[JarvisSecret]:
        return [
            JarvisSecret(
                "RESIDEO_CONSUMER_KEY",
                "Honeywell Home API consumer key (from developer.honeywellhome.com)",
                "integration", "string",
                required=True, is_sensitive=True,
                friendly_name="Consumer Key",
            ),
            JarvisSecret(
                "RESIDEO_CONSUMER_SECRET",
                "Honeywell Home API consumer secret",
                "integration", "string",
                required=True, is_sensitive=True,
                friendly_name="Consumer Secret",
            ),
            JarvisSecret(
                "RESIDEO_TEMP_UNIT",
                "Temperature unit: F (default) or C",
                "integration", "string",
                required=False, is_sensitive=False,
            ),
        ]

    @property
    def authentication(self) -> AuthenticationConfig:
        consumer_key: str = self._get_consumer_key() or ""
        consumer_secret: str = self._get_consumer_secret() or ""

        return AuthenticationConfig(
            type="oauth",
            provider="resideo",
            friendly_name="Resideo / Honeywell Home",
            client_id=consumer_key,
            client_secret=consumer_secret,
            keys=["access_token", "refresh_token"],
            authorize_url="https://api.honeywellhome.com/oauth2/authorize",
            exchange_url="https://api.honeywellhome.com/oauth2/token",
            scopes=[],
            supports_pkce=False,
            requires_background_refresh=True,
            refresh_interval_seconds=300,
            refresh_token_secret_key="RESIDEO_REFRESH_TOKEN",
        )

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(button_text="Heat", button_action="set_mode_heat", button_type="primary", button_icon="thermostat"),
            IJarvisButton(button_text="Cool", button_action="set_mode_cool", button_type="primary", button_icon="snowflake-variant"),
            IJarvisButton(button_text="Auto", button_action="set_mode_auto", button_type="primary", button_icon="autorenew"),
            IJarvisButton(button_text="Turn Off", button_action="turn_off", button_type="secondary", button_icon="thermostat-off"),
        ]

    def store_auth_values(self, values: dict[str, str]) -> None:
        if "access_token" in values:
            _storage.set_secret("RESIDEO_ACCESS_TOKEN", values["access_token"])
        if "refresh_token" in values:
            _storage.set_secret("RESIDEO_REFRESH_TOKEN", values["refresh_token"])
        try:
            from services.command_auth_service import clear_auth_flag
            clear_auth_flag("resideo")
        except ImportError:
            pass

    # ── helpers ──────────────────────────────────────────────────────

    def _api_headers(self) -> dict[str, str]:
        access_token: str = self._get_access_token() or ""
        return {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

    def _api_params(self, location_id: int | str | None = None) -> dict[str, str]:
        """Query params required on every Honeywell Home API call."""
        params: dict[str, str] = {"apikey": self._get_consumer_key() or ""}
        if location_id is not None:
            params["locationId"] = str(location_id)
        return params

    # ── discover ─────────────────────────────────────────────────────

    async def discover(self, timeout: int = 5) -> list[DiscoveredDevice]:
        consumer_key: str | None = self._get_consumer_key()
        access_token: str | None = self._get_access_token()

        if not consumer_key:
            logger.error("RESIDEO_CONSUMER_KEY not configured")
            return []
        if not access_token:
            logger.error("RESIDEO_ACCESS_TOKEN not configured — complete OAuth setup first")
            return []

        try:
            import httpx
        except ImportError:
            logger.error("httpx is not installed. Run: pip install httpx")
            return []

        devices: list[DiscoveredDevice] = []

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{API_BASE}/locations",
                    headers=self._api_headers(),
                    params=self._api_params(),
                )
                if resp.status_code == 401:
                    logger.error("Resideo access token expired — re-authenticate")
                    return []
                if resp.status_code != 200:
                    logger.error(f"Resideo API returned {resp.status_code}: {resp.text}")
                    return []

                locations: list[dict[str, Any]] = resp.json()

                for location in locations:
                    location_id: int = location.get("locationID", 0)
                    location_name: str = location.get("name", "")

                    for dev in location.get("devices", []):
                        device_class: str = dev.get("deviceClass", "")
                        if device_class != "Thermostat":
                            continue

                        device_id: str = dev.get("deviceID", "")
                        device_name: str = dev.get("userDefinedDeviceName", "") or dev.get("name", "")
                        if not device_name:
                            device_name = f"Thermostat {device_id}"

                        model: str = dev.get("deviceType", "Thermostat")
                        is_alive: bool = dev.get("isAlive", False)

                        slug: str = _slugify(device_name)
                        entity_id: str = f"climate.{slug}"

                        devices.append(
                            DiscoveredDevice(
                                entity_id=entity_id,
                                name=device_name,
                                domain="climate",
                                protocol=self.protocol_name,
                                model=model,
                                manufacturer="Resideo",
                                cloud_id=device_id,
                                is_controllable=is_alive,
                                extra={
                                    "location_id": location_id,
                                    "location_name": location_name,
                                    "is_alive": is_alive,
                                },
                            )
                        )

        except Exception as e:
            logger.error(f"Resideo discovery failed: {e}")
            return []

        logger.info(f"Resideo discovery found {len(devices)} device(s)")
        return devices

    # ── control ──────────────────────────────────────────────────────

    async def control(
        self, device: DiscoveredDevice, action: str, params: dict[str, Any] | None = None
    ) -> DeviceControlResult:
        access_token: str | None = self._get_access_token()
        if not access_token:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error="RESIDEO_ACCESS_TOKEN not configured — complete OAuth setup",
            )

        try:
            import httpx
        except ImportError:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error="httpx is not installed. Run: pip install httpx",
            )

        params = params or {}
        cloud_id: str = device.cloud_id or ""
        location_id: int | str = device.extra.get("location_id", "")
        if not cloud_id or not location_id:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error="Missing device ID or location ID",
            )

        # Fetch current state so we can preserve setpoints the user isn't changing
        current_state: dict[str, Any] = await self.get_state(
            "", cloud_id=cloud_id, location_id=location_id,
        ) or {}
        current_heat: float = current_state.get("heat_setpoint", 70)
        current_cool: float = current_state.get("cool_setpoint", 78)
        current_mode: str = current_state.get("mode", "Heat")

        payload: dict[str, Any] = {}

        if action == "set_temperature":
            temp: float = float(params.get("temperature", 72))
            mode: str = params.get("mode", current_mode)
            hold: str = params.get("hold", "PermanentHold")

            if mode.lower() in ("cool",):
                payload = {
                    "mode": "Cool",
                    "heatSetpoint": current_heat,
                    "coolSetpoint": temp,
                    "thermostatSetpointStatus": hold,
                }
            elif mode.lower() in ("auto",):
                heat_temp: float = float(params.get("heat_temperature", temp - 2))
                cool_temp: float = float(params.get("cool_temperature", temp + 2))
                payload = {
                    "mode": "Auto",
                    "autoChangeoverActive": True,
                    "heatSetpoint": heat_temp,
                    "coolSetpoint": cool_temp,
                    "thermostatSetpointStatus": hold,
                }
            else:
                # Default: Heat
                payload = {
                    "mode": "Heat",
                    "heatSetpoint": temp,
                    "coolSetpoint": current_cool,
                    "thermostatSetpointStatus": hold,
                }

        elif action == "set_mode":
            mode_val: str = str(params.get("mode", "Heat")).capitalize()
            valid_modes: set[str] = {"Heat", "Cool", "Off", "Auto"}
            if mode_val not in valid_modes:
                return DeviceControlResult(
                    success=False, entity_id=device.entity_id, action=action,
                    error=f"Invalid mode: {mode_val}. Valid: {', '.join(sorted(valid_modes))}",
                )
            payload = {
                "mode": mode_val,
                "heatSetpoint": current_heat,
                "coolSetpoint": current_cool,
            }
            if mode_val == "Auto":
                payload["autoChangeoverActive"] = True

        elif action == "set_mode_heat":
            payload = {"mode": "Heat", "heatSetpoint": current_heat, "coolSetpoint": current_cool}

        elif action == "set_mode_cool":
            payload = {"mode": "Cool", "heatSetpoint": current_heat, "coolSetpoint": current_cool}

        elif action == "set_mode_auto":
            payload = {"mode": "Auto", "autoChangeoverActive": True, "heatSetpoint": current_heat, "coolSetpoint": current_cool}

        elif action == "turn_off":
            payload = {"mode": "Off", "heatSetpoint": current_heat, "coolSetpoint": current_cool}

        elif action == "turn_on":
            # Resume the previous non-off mode, default to Heat
            resume_mode: str = current_mode if current_mode != "Off" else "Heat"
            payload = {"mode": resume_mode, "heatSetpoint": current_heat, "coolSetpoint": current_cool}

        elif action == "set_fan":
            return await self._set_fan(device, params, cloud_id, location_id)

        elif action == "resume_schedule":
            payload = {
                "mode": current_mode if current_mode != "Off" else "Heat",
                "heatSetpoint": current_heat,
                "coolSetpoint": current_cool,
                "thermostatSetpointStatus": "NoHold",
            }

        else:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error=f"Unsupported action: {action}",
            )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{API_BASE}/devices/thermostats/{cloud_id}",
                    headers=self._api_headers(),
                    params=self._api_params(location_id),
                    json=payload,
                )
                if resp.status_code == 401:
                    return DeviceControlResult(
                        success=False, entity_id=device.entity_id, action=action,
                        error="Resideo access token expired — re-authenticate",
                    )
                if resp.status_code in (200, 202):
                    return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)
                else:
                    return DeviceControlResult(
                        success=False, entity_id=device.entity_id, action=action,
                        error=f"Resideo API returned {resp.status_code}: {resp.text}",
                    )
        except Exception as e:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error=f"Control failed: {e}",
            )

    async def _set_fan(
        self,
        device: DiscoveredDevice,
        params: dict[str, Any],
        cloud_id: str,
        location_id: int | str,
    ) -> DeviceControlResult:
        """Change the fan mode (Auto, On, Circulate)."""
        try:
            import httpx
        except ImportError:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action="set_fan",
                error="httpx is not installed",
            )

        fan_mode: str = str(params.get("mode", "Auto")).capitalize()
        valid_fan_modes: set[str] = {"Auto", "On", "Circulate"}
        if fan_mode not in valid_fan_modes:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action="set_fan",
                error=f"Invalid fan mode: {fan_mode}. Valid: {', '.join(sorted(valid_fan_modes))}",
            )

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{API_BASE}/devices/thermostats/{cloud_id}/fan",
                    headers=self._api_headers(),
                    params=self._api_params(location_id),
                    json={"mode": fan_mode},
                )
                if resp.status_code in (200, 202):
                    return DeviceControlResult(success=True, entity_id=device.entity_id, action="set_fan")
                else:
                    return DeviceControlResult(
                        success=False, entity_id=device.entity_id, action="set_fan",
                        error=f"Fan API returned {resp.status_code}: {resp.text}",
                    )
        except Exception as e:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action="set_fan",
                error=f"Fan control failed: {e}",
            )

    # ── get_state ────────────────────────────────────────────────────

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        access_token: str | None = self._get_access_token()
        if not access_token:
            return {"error": "RESIDEO_ACCESS_TOKEN not configured"}

        try:
            import httpx
        except ImportError:
            return {"error": "httpx is not installed"}

        cloud_id: str = kwargs.get("cloud_id", "")
        location_id: int | str = kwargs.get("location_id", "")

        if not cloud_id or not location_id:
            device = kwargs.get("device")
            if device and hasattr(device, "cloud_id"):
                cloud_id = cloud_id or (device.cloud_id or "")
            if device and hasattr(device, "extra"):
                location_id = location_id or device.extra.get("location_id", "")

        if not cloud_id:
            return {"error": "No device ID available"}
        if not location_id:
            return {"error": "No location ID available"}

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{API_BASE}/devices/thermostats/{cloud_id}",
                    headers=self._api_headers(),
                    params=self._api_params(location_id),
                )
                if resp.status_code == 401:
                    return {"error": "Resideo access token expired — re-authenticate"}
                if resp.status_code != 200:
                    return {"error": f"Resideo API returned {resp.status_code}"}

                data: dict[str, Any] = resp.json()
                return self._parse_thermostat_state(data)

        except Exception as e:
            return {"error": f"Failed to get state: {e}"}

    def _parse_thermostat_state(self, data: dict[str, Any]) -> dict[str, Any]:
        """Parse raw Honeywell Home thermostat response into normalized state."""
        changeable: dict[str, Any] = data.get("changeableValues", {})
        operation: dict[str, Any] = data.get("operationStatus", {})
        settings: dict[str, Any] = data.get("settings", {})
        fan_settings: dict[str, Any] = settings.get("fan", {})
        fan_changeable: dict[str, Any] = fan_settings.get("changeableValues", {})

        mode: str = changeable.get("mode", "Off")

        state: dict[str, Any] = {
            "state": "off" if mode == "Off" else "on",
            "mode": mode,
            "current_temperature": data.get("indoorTemperature"),
            "humidity": data.get("indoorHumidity"),
            "heat_setpoint": changeable.get("heatSetpoint"),
            "cool_setpoint": changeable.get("coolSetpoint"),
            "hold_status": changeable.get("thermostatSetpointStatus", "NoHold"),
            "allowed_modes": data.get("allowedModes", []),
            "is_alive": data.get("isAlive", False),
            "temperature_unit": data.get("units", "Fahrenheit"),
        }

        # Set target_temperature based on active mode
        if mode == "Cool" and state["cool_setpoint"] is not None:
            state["target_temperature"] = state["cool_setpoint"]
        elif state["heat_setpoint"] is not None:
            state["target_temperature"] = state["heat_setpoint"]

        # Outdoor conditions
        outdoor_temp: Any = data.get("outdoorTemperature")
        if outdoor_temp is not None:
            state["outdoor_temperature"] = outdoor_temp
        outdoor_humidity: Any = data.get("displayedOutdoorHumidity")
        if outdoor_humidity is not None:
            state["outdoor_humidity"] = outdoor_humidity

        # HVAC operation status
        if operation:
            state["hvac_status"] = operation.get("mode", "Unknown")
            state["fan_running"] = operation.get("fanRequest", False)

        # Fan mode
        if fan_changeable:
            state["fan_mode"] = fan_changeable.get("mode", "Auto")
        if fan_settings.get("allowedModes"):
            state["allowed_fan_modes"] = fan_settings["allowedModes"]

        # Setpoint limits
        min_heat: Any = data.get("minHeatSetpoint")
        max_heat: Any = data.get("maxHeatSetpoint")
        min_cool: Any = data.get("minCoolSetpoint")
        max_cool: Any = data.get("maxCoolSetpoint")
        if min_heat is not None:
            state["min_heat_setpoint"] = min_heat
        if max_heat is not None:
            state["max_heat_setpoint"] = max_heat
        if min_cool is not None:
            state["min_cool_setpoint"] = min_cool
        if max_cool is not None:
            state["max_cool_setpoint"] = max_cool

        # Schedule info
        hold_until: str | None = changeable.get("nextPeriodTime")
        if hold_until and state["hold_status"] == "HoldUntil":
            state["hold_until"] = hold_until

        return state
