"""Google Nest device family adapter (cloud-only via SDM API).

Controls Nest thermostat and discovers Nest cameras/doorbells via the
Smart Device Management (SDM) API. Requires a Device Access project
($5 one-time) and OAuth consent via the mobile app.

SDM API docs: https://developers.google.com/nest/device-access/api
"""

import re
from typing import Any, Literal

import httpx
from jarvis_log_client import JarvisLogger

from core.ijarvis_authentication import AuthenticationConfig
from core.ijarvis_button import IJarvisButton
from core.ijarvis_secret import JarvisSecret
from device_families.base import (
    DeviceControlResult,
    IJarvisDeviceProtocol,
    DiscoveredDevice,
)

logger = JarvisLogger(service="jarvis-node")

SDM_API_BASE = "https://smartdevicemanagement.googleapis.com/v1"

DEFAULT_CLIENT_ID = (
    "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec.apps.googleusercontent.com"
)

_SDM_TYPE_TO_DOMAIN: dict[str, str] = {
    "sdm.devices.types.THERMOSTAT": "climate",
    "sdm.devices.types.CAMERA": "camera",
    "sdm.devices.types.DOORBELL": "camera",
    "sdm.devices.types.DISPLAY": "camera",
}

_SDM_TYPE_TO_MODEL: dict[str, str] = {
    "sdm.devices.types.THERMOSTAT": "Thermostat",
    "sdm.devices.types.CAMERA": "Camera",
    "sdm.devices.types.DOORBELL": "Doorbell",
    "sdm.devices.types.DISPLAY": "Display",
}


def _slugify(name: str) -> str:
    """Convert device name to HA-style entity slug."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _f_to_c(f: float) -> float:
    """Convert Fahrenheit to Celsius."""
    return (f - 32) * 5 / 9


def _c_to_f(c: float) -> float:
    """Convert Celsius to Fahrenheit."""
    return c * 9 / 5 + 32


def _get_secret(key: str) -> str | None:
    """Load a secret from the secrets DB."""
    from services.secret_service import get_secret_value

    return get_secret_value(key, "integration")


def _extract_device_name(device: dict[str, Any]) -> str:
    """Extract human-readable name from SDM device traits."""
    traits: dict[str, Any] = device.get("traits", {})
    info: dict[str, Any] = traits.get("sdm.devices.traits.Info", {})
    custom_name: str = info.get("customName", "")
    if custom_name:
        return custom_name

    # Fallback to device type name
    device_type: str = device.get("type", "")
    return _SDM_TYPE_TO_MODEL.get(device_type, "Nest Device")


class NestProtocol(IJarvisDeviceProtocol):
    """Google Nest cloud protocol via Smart Device Management API."""

    @property
    def protocol_name(self) -> str:
        return "nest"

    @property
    def friendly_name(self) -> str:
        return "Google Nest"

    @property
    def description(self) -> str:
        return "Google Nest devices (thermostat, camera, doorbell) via SDM API"

    @property
    def supported_domains(self) -> list[str]:
        return ["climate", "camera"]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton("Set Temperature", "set_temperature", "primary", "thermometer"),
            IJarvisButton("Set Mode", "set_mode", "secondary", "settings"),
        ]

    @property
    def connection_type(self) -> Literal["lan", "cloud", "hybrid"]:
        return "cloud"

    @property
    def required_secrets(self) -> list[JarvisSecret]:
        return [
            JarvisSecret(
                key="NEST_PROJECT_ID",
                description="SDM Device Access project ID (from console.nest.google.com/device-access)",
                scope="integration",
                value_type="string",
                required=True,
                is_sensitive=False,
                friendly_name="Nest Project ID",
            ),
            JarvisSecret(
                key="NEST_ACCESS_TOKEN",
                description="OAuth access token (auto-populated by OAuth flow)",
                scope="integration",
                value_type="string",
                required=False,
                is_sensitive=True,
                friendly_name="Nest Access Token",
            ),
            JarvisSecret(
                key="NEST_REFRESH_TOKEN",
                description="OAuth refresh token (auto-populated by OAuth flow)",
                scope="integration",
                value_type="string",
                required=False,
                is_sensitive=True,
                friendly_name="Nest Refresh Token",
            ),
            JarvisSecret(
                key="NEST_CLIENT_ID",
                description="Override default Google OAuth client ID (optional)",
                scope="integration",
                value_type="string",
                required=False,
                is_sensitive=False,
                friendly_name="Nest Client ID",
            ),
            JarvisSecret(
                key="NEST_TEMP_UNIT",
                description="Temperature unit: F (default) or C",
                scope="integration",
                value_type="string",
                required=False,
                is_sensitive=False,
                friendly_name="Temperature Unit",
            ),
        ]

    @property
    def authentication(self) -> AuthenticationConfig:
        # SDM requires the Nest-specific authorize URL which includes the
        # project ID. This URL triggers the device/home selection screen
        # that lets the user choose which Nest devices to share.
        project_id = self._get_project_id()
        if project_id:
            authorize_url = (
                f"https://nestservices.google.com/partnerconnections/{project_id}/auth"
            )
        else:
            # Fallback — won't show device selection but still allows OAuth
            authorize_url = "https://nestservices.google.com/partnerconnections/auth"

        return AuthenticationConfig(
            type="oauth",
            provider="google_nest",
            friendly_name="Google Nest",
            client_id=self._get_client_id(),
            keys=["access_token", "refresh_token"],
            authorize_url=authorize_url,
            exchange_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/sdm.service"],
            supports_pkce=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            requires_background_refresh=True,
            refresh_token_secret_key="NEST_REFRESH_TOKEN",
            native_redirect_uri=(
                "com.googleusercontent.apps."
                "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec:/oauthredirect"
            ),
        )

    def store_auth_values(self, values: dict[str, str]) -> None:
        """Store OAuth tokens from the mobile app's OAuth flow."""
        from services.command_auth_service import clear_auth_flag
        from services.secret_service import set_secret

        if "access_token" in values:
            set_secret("NEST_ACCESS_TOKEN", values["access_token"], "integration")
        if "refresh_token" in values:
            set_secret("NEST_REFRESH_TOKEN", values["refresh_token"], "integration")
        clear_auth_flag("google_nest")

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        """Discover Nest devices via the SDM API."""
        project_id = self._get_project_id()
        token = self._get_access_token()

        if not project_id:
            logger.debug("NEST_PROJECT_ID not set, skipping Nest discovery")
            return []
        if not token:
            logger.debug("NEST_ACCESS_TOKEN not set, skipping Nest discovery")
            return []

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{SDM_API_BASE}/enterprises/{project_id}/devices",
                    headers=self._get_headers(token),
                )
                resp.raise_for_status()
                body = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.warning("Nest API 401 — re-authenticate via mobile app")
            else:
                logger.error(
                    "Nest API error during discovery",
                    status=e.response.status_code,
                    body=e.response.text[:200],
                )
            return []
        except Exception as e:
            logger.error("Nest discovery failed", error=str(e))
            return []

        devices_data: list[dict[str, Any]] = body.get("devices", [])
        results: list[DiscoveredDevice] = []

        for dev in devices_data:
            device_type: str = dev.get("type", "")
            domain = _SDM_TYPE_TO_DOMAIN.get(device_type)
            if domain is None:
                logger.debug("Unknown Nest device type, skipping", device_type=device_type)
                continue

            device_name = _extract_device_name(dev)
            model = _SDM_TYPE_TO_MODEL.get(device_type, "Unknown")
            cloud_id: str = dev.get("name", "")  # Full SDM device path
            slug = _slugify(device_name)
            entity_id = f"{domain}.{slug}"

            # Thermostats are controllable, cameras are not (read-only + stream)
            is_controllable = domain == "climate"

            # Set device_class for doorbells
            device_class: str | None = None
            if device_type == "sdm.devices.types.DOORBELL":
                device_class = "doorbell"

            results.append(DiscoveredDevice(
                name=device_name,
                domain=domain,
                manufacturer="Google",
                model=model,
                protocol="nest",
                entity_id=entity_id,
                cloud_id=cloud_id,
                is_controllable=is_controllable,
                device_class=device_class,
                extra={"device_type": device_type},
            ))

        logger.info("Nest discovery complete", device_count=len(results))
        return results

    async def control(
        self,
        ip: str,
        action: str,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DeviceControlResult:
        """Control a Nest device via the SDM API."""
        token = self._get_access_token()
        entity_id: str = kwargs.get("entity_id", "")
        cloud_id: str = kwargs.get("cloud_id", "")

        if not token:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error="NEST_ACCESS_TOKEN not configured — authenticate via mobile app",
            )
        if not cloud_id:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error="cloud_id is required for Nest device control",
            )

        command: str | None = None
        params: dict[str, Any] = {}

        if action == "set_temperature":
            if not data or "temperature" not in data:
                return DeviceControlResult(
                    success=False, entity_id=entity_id, action=action,
                    error="temperature value is required",
                )
            temp_value = float(data["temperature"])
            temp_c = self._to_celsius(temp_value)

            # Determine setpoint command based on current mode
            mode = await self._get_current_mode(cloud_id, token)
            if mode == "COOL":
                command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
                params = {"coolCelsius": round(temp_c, 1)}
            else:
                # Default to heat for HEAT, HEATCOOL, or unknown mode
                command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat"
                params = {"heatCelsius": round(temp_c, 1)}

        elif action == "set_mode":
            if not data or "mode" not in data:
                return DeviceControlResult(
                    success=False, entity_id=entity_id, action=action,
                    error="mode value is required",
                )
            mode_value = data["mode"].upper()
            if mode_value not in ("HEAT", "COOL", "HEATCOOL", "OFF"):
                return DeviceControlResult(
                    success=False, entity_id=entity_id, action=action,
                    error=f"Unsupported mode: {data['mode']}. Use heat, cool, heatcool, or off",
                )
            command = "sdm.devices.commands.ThermostatMode.SetMode"
            params = {"mode": mode_value}

        elif action == "get_stream":
            command = "sdm.devices.commands.CameraLiveStream.GenerateRtspStream"
            params = {}

        else:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=f"Unsupported action: {action}",
            )

        payload: dict[str, Any] = {
            "command": command,
            "params": params,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{SDM_API_BASE}/{cloud_id}:executeCommand",
                    json=payload,
                    headers=self._get_headers(token),
                )
                resp.raise_for_status()
                body = resp.json()

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                error_msg = "Nest API 401 — re-authenticate via mobile app"
            else:
                error_msg = f"Nest API {e.response.status_code}: {e.response.text[:100]}"
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=error_msg,
            )
        except Exception as e:
            return DeviceControlResult(
                success=False, entity_id=entity_id, action=action,
                error=str(e),
            )

        # For stream requests, include the URL in the result
        result = DeviceControlResult(success=True, entity_id=entity_id, action=action)
        if action == "get_stream" and "results" in body:
            stream_urls = body["results"]
            result.error = None  # No error
            # Store stream URL in extra info via error field (protocol convention)
            if "streamUrls" in stream_urls:
                rtsp_url = stream_urls["streamUrls"].get("rtspUrl", "")
                if rtsp_url:
                    logger.info("Nest camera stream generated", entity_id=entity_id)
        return result

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        """Query current Nest device state via the SDM API."""
        token = self._get_access_token()
        cloud_id: str = kwargs.get("cloud_id", "")

        if not token or not cloud_id:
            return None

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{SDM_API_BASE}/{cloud_id}",
                    headers=self._get_headers(token),
                )
                resp.raise_for_status()
                device = resp.json()
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                logger.warning("Nest API 401 during state query — re-authenticate")
            else:
                logger.warning(
                    "Nest state query failed",
                    status=e.response.status_code,
                    device=cloud_id,
                )
            return None
        except Exception as e:
            logger.warning("Nest state query failed", error=str(e), device=cloud_id)
            return None

        device_type: str = device.get("type", "")
        domain = _SDM_TYPE_TO_DOMAIN.get(device_type, "")
        traits: dict[str, Any] = device.get("traits", {})

        if domain == "climate":
            return self._parse_thermostat_state(traits)
        elif domain == "camera":
            return self._parse_camera_state(traits)
        return None

    def _parse_thermostat_state(self, traits: dict[str, Any]) -> dict[str, Any]:
        """Parse thermostat state from SDM traits."""
        state: dict[str, Any] = {}

        # Connectivity
        connectivity = traits.get("sdm.devices.traits.Connectivity", {})
        online = connectivity.get("status") == "ONLINE"
        state["online"] = online

        # HVAC status
        hvac = traits.get("sdm.devices.traits.ThermostatHvac", {})
        hvac_status: str = hvac.get("status", "OFF")
        state["state"] = hvac_status.lower()  # "heating", "cooling", "off"

        # Mode
        mode_trait = traits.get("sdm.devices.traits.ThermostatMode", {})
        mode: str = mode_trait.get("mode", "OFF")
        state["mode"] = mode

        # Current temperature
        temp_trait = traits.get("sdm.devices.traits.Temperature", {})
        ambient_c: float | None = temp_trait.get("ambientTemperatureCelsius")
        if ambient_c is not None:
            state["current_temperature_c"] = round(ambient_c, 1)
            state["current_temperature_f"] = round(_c_to_f(ambient_c), 0)

        # Target temperature (setpoint)
        setpoint = traits.get("sdm.devices.traits.ThermostatTemperatureSetpoint", {})
        heat_c: float | None = setpoint.get("heatCelsius")
        cool_c: float | None = setpoint.get("coolCelsius")
        target_c = heat_c if heat_c is not None else cool_c
        if target_c is not None:
            state["target_temperature_c"] = round(target_c, 1)
            state["target_temperature_f"] = round(_c_to_f(target_c), 0)

        # Humidity
        humidity_trait = traits.get("sdm.devices.traits.Humidity", {})
        humidity: float | None = humidity_trait.get("ambientHumidityPercent")
        if humidity is not None:
            state["humidity"] = round(humidity)

        return state

    def _parse_camera_state(self, traits: dict[str, Any]) -> dict[str, Any]:
        """Parse camera/doorbell state from SDM traits."""
        connectivity = traits.get("sdm.devices.traits.Connectivity", {})
        online = connectivity.get("status") == "ONLINE"
        return {
            "state": "online" if online else "offline",
            "online": online,
        }

    async def _get_current_mode(self, cloud_id: str, token: str) -> str:
        """Get the thermostat's current mode to determine setpoint command."""
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(
                    f"{SDM_API_BASE}/{cloud_id}",
                    headers=self._get_headers(token),
                )
                resp.raise_for_status()
                device = resp.json()
            traits = device.get("traits", {})
            mode_trait = traits.get("sdm.devices.traits.ThermostatMode", {})
            return mode_trait.get("mode", "HEAT")
        except Exception as e:
            logger.warning("Failed to get thermostat mode, defaulting to HEAT", error=str(e))
            return "HEAT"

    def _get_project_id(self) -> str | None:
        return _get_secret("NEST_PROJECT_ID")

    def _get_access_token(self) -> str | None:
        return _get_secret("NEST_ACCESS_TOKEN")

    def _get_client_id(self) -> str:
        return _get_secret("NEST_CLIENT_ID") or DEFAULT_CLIENT_ID

    def _get_temp_unit(self) -> str:
        unit = _get_secret("NEST_TEMP_UNIT")
        return unit.upper() if unit and unit.upper() in ("F", "C") else "F"

    def _to_celsius(self, value: float) -> float:
        """Convert user-facing temperature to Celsius for the API."""
        if self._get_temp_unit() == "C":
            return value
        return _f_to_c(value)

    def _get_headers(self, token: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
