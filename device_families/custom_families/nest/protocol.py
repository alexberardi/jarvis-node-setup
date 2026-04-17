"""Google Nest protocol adapter (SDM API)."""

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


logger = JarvisLogger(service="device.nest")

_storage = JarvisStorage("nest")

DEFAULT_CLIENT_ID: str = "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec.apps.googleusercontent.com"
SDM_API_BASE: str = "https://smartdevicemanagement.googleapis.com/v1"

_SDM_TYPE_TO_DOMAIN: dict[str, str] = {
    "sdm.devices.types.THERMOSTAT": "climate",
    "sdm.devices.types.CAMERA": "camera",
    "sdm.devices.types.DOORBELL": "camera",
    "sdm.devices.types.DISPLAY": "camera",
}

_SDM_TYPE_TO_MODEL: dict[str, str] = {
    "sdm.devices.types.THERMOSTAT": "Nest Thermostat",
    "sdm.devices.types.CAMERA": "Nest Cam",
    "sdm.devices.types.DOORBELL": "Nest Doorbell",
    "sdm.devices.types.DISPLAY": "Nest Hub Max",
}


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def _c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _extract_device_name(traits: dict[str, Any]) -> str:
    info: dict[str, Any] = traits.get("sdm.devices.traits.Info", {})
    custom_name: str = info.get("customName", "")
    if custom_name:
        return custom_name

    room_info: dict[str, Any] = traits.get("sdm.structures.traits.RoomInfo", {})
    room_name: str = room_info.get("customName", "")
    if room_name:
        return room_name

    return ""


class NestProtocol(IJarvisDeviceProtocol):
    """Google Nest SDM API protocol adapter."""

    protocol_name: str = "nest"
    friendly_name: str = "Google Nest"
    supported_domains: list[str] = ["climate", "camera"]
    connection_type: str = "cloud"

    def _get_project_id(self) -> str | None:
        return _storage.get_secret("NEST_PROJECT_ID")

    def _get_access_token(self) -> str | None:
        return _storage.get_secret("NEST_ACCESS_TOKEN")

    def _get_temp_unit(self) -> str:
        unit: str | None = _storage.get_secret("NEST_TEMP_UNIT")
        if unit and unit.upper() in ("C", "F"):
            return unit.upper()
        return "F"

    def _get_client_id(self) -> str:
        override: str | None = _storage.get_secret("NEST_CLIENT_ID")
        return override if override else DEFAULT_CLIENT_ID

    def _get_web_client_id(self) -> str | None:
        return _storage.get_secret("NEST_WEB_CLIENT_ID")

    def _get_web_client_secret(self) -> str | None:
        return _storage.get_secret("NEST_WEB_CLIENT_SECRET")

    def _has_camera_support(self) -> bool:
        """Camera support requires Web Application OAuth credentials."""
        return bool(self._get_web_client_id() and self._get_web_client_secret())

    @property
    def required_secrets(self) -> list[JarvisSecret]:
        base: list[JarvisSecret] = [
            JarvisSecret("NEST_PROJECT_ID", "SDM Device Access project ID", "integration", "string", required=True, is_sensitive=False, friendly_name="Project ID"),
            JarvisSecret("NEST_TEMP_UNIT", "Temperature unit: F or C (default F)", "integration", "string", required=False, is_sensitive=False, friendly_name="Temperature Unit"),
            JarvisSecret(
                "NEST_CAMERA_SUPPORT", "Enable live camera/doorbell streaming",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="Camera Support",
                enum_values=["off", "on"],
            ),
        ]

        if _storage.get_secret("NEST_CAMERA_SUPPORT") == "on":
            base.extend([
                JarvisSecret("NEST_WEB_CLIENT_ID", "Web Application OAuth client ID", "integration", "string", required=True, is_sensitive=False, friendly_name="Web Client ID"),
                JarvisSecret("NEST_WEB_CLIENT_SECRET", "Web Application OAuth client secret", "integration", "string", required=True, friendly_name="Web Client Secret"),
            ])

        return base

    @property
    def authentication(self) -> AuthenticationConfig:
        project_id = self._get_project_id()
        if project_id:
            authorize_url = (
                f"https://nestservices.google.com/partnerconnections/{project_id}/auth"
            )
        else:
            authorize_url = "https://nestservices.google.com/partnerconnections/auth"

        if self._has_camera_support():
            # Web Application OAuth — token works with go2rtc for camera streaming.
            # Web clients require https:// redirect URIs, so we use the relay
            # bounce flow (no native_redirect_uri).
            web_client_id: str = self._get_web_client_id()  # type: ignore[assignment]
            web_client_secret: str = self._get_web_client_secret()  # type: ignore[assignment]
            return AuthenticationConfig(
                type="oauth",
                provider="google_nest",
                friendly_name="Google Nest",
                client_id=web_client_id,
                client_secret=web_client_secret,
                keys=["access_token", "refresh_token"],
                authorize_url=authorize_url,
                exchange_url="https://oauth2.googleapis.com/token",
                scopes=["https://www.googleapis.com/auth/sdm.service"],
                supports_pkce=False,
                extra_authorize_params={"access_type": "offline", "prompt": "consent"},
                requires_background_refresh=True,
                refresh_token_secret_key="NEST_REFRESH_TOKEN",
            )

        # Default: iOS/PKCE flow — thermostat only, no camera streaming
        client_id: str = self._get_client_id()
        return AuthenticationConfig(
            type="oauth",
            provider="google_nest",
            friendly_name="Google Nest",
            client_id=client_id,
            keys=["access_token", "refresh_token"],
            authorize_url=authorize_url,
            exchange_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/sdm.service"],
            supports_pkce=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            requires_background_refresh=True,
            refresh_token_secret_key="NEST_REFRESH_TOKEN",
            native_redirect_uri=(
                f"com.googleusercontent.apps."
                f"{client_id.removesuffix('.apps.googleusercontent.com')}:/oauthredirect"
            ),
        )

    @property
    def setup_guide(self) -> str | None:
        return (
            "## Basic Setup (Thermostat)\n\n"
            "1. Go to [Google Device Access Console](https://console.nest.google.com/device-access)\n"
            "2. Create a project (one-time $5 fee) and copy the **Project ID**\n"
            "3. Paste it in the **NEST_PROJECT_ID** field above\n"
            "4. Tap **Authenticate with Google Nest** and sign in\n\n"
            "That's it — your thermostat will appear in device discovery.\n\n"
            "## Camera Support (Optional)\n\n"
            "To view live camera/doorbell streams, enable **Camera Support** above and provide "
            "a **Web Application** OAuth client:\n\n"
            "1. Go to [Google Cloud Console → Credentials](https://console.cloud.google.com/apis/credentials)\n"
            "2. Click **Create Credentials → OAuth client ID**\n"
            "3. Select **Web application** as the type\n"
            "4. Under **Authorized redirect URIs**, add:\n"
            "   `https://relay.jarvisautomation.io/oauth/bounce`\n"
            "5. Click **Create** and copy the **Client ID** and **Client Secret**\n"
            "6. Paste them in the fields above\n"
            "7. **Important:** Update your Device Access project's OAuth client ID "
            "to the new Web Application client ID\n"
            "8. Re-authenticate with **Authenticate with Google Nest**\n\n"
            "The new token works for both thermostat and camera control."
        )

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(button_text="Turn On", button_action="turn_on", button_type="primary", button_icon="thermostat"),
            IJarvisButton(button_text="Turn Off", button_action="turn_off", button_type="secondary", button_icon="thermostat-off"),
        ]

    def store_auth_values(self, values: dict[str, str]) -> None:
        if "access_token" in values:
            _storage.set_secret("NEST_ACCESS_TOKEN", values["access_token"])
        if "refresh_token" in values:
            _storage.set_secret("NEST_REFRESH_TOKEN", values["refresh_token"])
        try:
            from services.command_auth_service import clear_auth_flag
            clear_auth_flag("google_nest")
        except ImportError:
            pass

    async def discover(self, timeout: int = 5) -> list[DiscoveredDevice]:
        project_id: str | None = self._get_project_id()
        access_token: str | None = self._get_access_token()

        if not project_id:
            logger.error("NEST_PROJECT_ID not configured")
            return []
        if not access_token:
            logger.error("NEST_ACCESS_TOKEN not configured — complete OAuth setup first")
            return []

        try:
            import httpx
        except ImportError:
            logger.error("httpx is not installed. Run: pip install httpx")
            return []

        devices: list[DiscoveredDevice] = []
        headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(
                    f"{SDM_API_BASE}/enterprises/{project_id}/devices",
                    headers=headers,
                )
                if resp.status_code == 401:
                    logger.error("Nest access token expired — re-authenticate")
                    return []
                if resp.status_code != 200:
                    logger.error(f"Nest API returned {resp.status_code}: {resp.text}")
                    return []

                data: dict[str, Any] = resp.json()
                raw_devices: list[dict[str, Any]] = data.get("devices", [])

                for dev in raw_devices:
                    device_type: str = dev.get("type", "")
                    domain: str = _SDM_TYPE_TO_DOMAIN.get(device_type, "")
                    if not domain:
                        continue

                    cloud_id: str = dev.get("name", "")
                    traits: dict[str, Any] = dev.get("traits", {})
                    device_name: str = _extract_device_name(traits)
                    model: str = _SDM_TYPE_TO_MODEL.get(device_type, "Nest Device")

                    if not device_name:
                        device_name = model

                    slug: str = _slugify(device_name) if device_name else _slugify(cloud_id)
                    device_id: str = f"{domain}.{slug}"

                    devices.append(
                        DiscoveredDevice(
                            entity_id=device_id,
                            name=device_name,
                            domain=domain,
                            protocol=self.protocol_name,
                            model=model,
                            manufacturer="Google",
                            cloud_id=cloud_id,
                            extra={"sdm_type": device_type},
                        )
                    )

        except Exception as e:
            logger.error(f"Nest discovery failed: {e}")
            return []

        logger.info(f"Nest discovery found {len(devices)} device(s)")
        return devices

    async def _get_current_mode(self, access_token: str, cloud_id: str) -> str:
        """Query the Nest API for the thermostat's current mode."""
        try:
            import httpx

            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{SDM_API_BASE}/{cloud_id}",
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                if resp.status_code == 200:
                    traits = resp.json().get("traits", {})
                    mode_trait = traits.get("sdm.devices.traits.ThermostatMode", {})
                    return mode_trait.get("mode", "HEAT").lower()
        except Exception:
            pass
        return "heat"

    async def control(
        self, device: DiscoveredDevice, action: str, params: dict[str, Any] | None = None
    ) -> DeviceControlResult:
        access_token: str | None = self._get_access_token()
        if not access_token:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error="NEST_ACCESS_TOKEN not configured — complete OAuth setup",
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
        if not cloud_id:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error="No cloud device ID available")

        headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        command: str = ""
        command_params: dict[str, Any] = {}
        temp_unit: str = self._get_temp_unit()

        if action == "set_temperature":
            temp_value: float = float(params.get("temperature", 72))

            if temp_unit == "F":
                temp_celsius: float = _f_to_c(temp_value)
            else:
                temp_celsius = temp_value

            # Determine mode: explicit param, or query the device
            mode: str = params.get("setpoint_mode", "").lower()
            if not mode:
                mode = await self._get_current_mode(access_token, cloud_id)

            if mode == "off":
                # Can't set temp while OFF — switch to HEAT first
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=10) as pre_client:
                        await pre_client.post(
                            f"{SDM_API_BASE}/{cloud_id}:executeCommand",
                            headers=headers,
                            json={
                                "command": "sdm.devices.commands.ThermostatMode.SetMode",
                                "params": {"mode": "HEAT"},
                            },
                        )
                    mode = "heat"
                except Exception:
                    pass

            if mode == "cool":
                command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetCool"
                command_params = {"coolCelsius": round(temp_celsius, 1)}
            elif mode == "heatcool":
                heat_temp: float = float(params.get("heat_temperature", temp_value - 2))
                cool_temp: float = float(params.get("cool_temperature", temp_value + 2))
                if temp_unit == "F":
                    heat_celsius: float = _f_to_c(heat_temp)
                    cool_celsius: float = _f_to_c(cool_temp)
                else:
                    heat_celsius = heat_temp
                    cool_celsius = cool_temp
                command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetRange"
                command_params = {
                    "heatCelsius": round(heat_celsius, 1),
                    "coolCelsius": round(cool_celsius, 1),
                }
            else:
                # Default: HEAT mode
                command = "sdm.devices.commands.ThermostatTemperatureSetpoint.SetHeat"
                command_params = {"heatCelsius": round(temp_celsius, 1)}

        elif action == "set_mode":
            nest_mode: str = str(params.get("mode", "HEAT")).upper()
            valid_modes: set[str] = {"HEAT", "COOL", "HEATCOOL", "OFF"}
            if nest_mode not in valid_modes:
                return DeviceControlResult(
                    success=False, entity_id=device.entity_id, action=action,
                    error=f"Invalid mode: {nest_mode}. Valid: {', '.join(sorted(valid_modes))}",
                )
            command = "sdm.devices.commands.ThermostatMode.SetMode"
            command_params = {"mode": nest_mode}

        elif action == "turn_on":
            command = "sdm.devices.commands.ThermostatMode.SetMode"
            command_params = {"mode": "HEAT"}

        elif action == "turn_off":
            command = "sdm.devices.commands.ThermostatMode.SetMode"
            command_params = {"mode": "OFF"}

        elif action == "get_stream":
            command = "sdm.devices.commands.CameraLiveStream.GenerateRtspStream"
            command_params = {}

        else:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Unsupported action: {action}")

        payload: dict[str, Any] = {
            "command": command,
            "params": command_params,
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.post(
                    f"{SDM_API_BASE}/{cloud_id}:executeCommand",
                    headers=headers,
                    json=payload,
                )
                if resp.status_code == 401:
                    return DeviceControlResult(
                        success=False, entity_id=device.entity_id, action=action, error="Nest access token expired — re-authenticate"
                    )
                if resp.status_code == 200:
                    result_data: dict[str, Any] = resp.json()

                    if action == "get_stream":
                        results: dict[str, Any] = result_data.get("results", {})
                        stream_url: str = results.get("streamUrls", {}).get("rtspUrl", "")
                        return DeviceControlResult(
                            success=True, entity_id=device.entity_id, action=action,
                        )

                    return DeviceControlResult(success=True, entity_id=device.entity_id, action=action)
                else:
                    return DeviceControlResult(
                        success=False, entity_id=device.entity_id, action=action,
                        error=f"Nest API returned {resp.status_code}: {resp.text}",
                    )
        except Exception as e:
            return DeviceControlResult(success=False, entity_id=device.entity_id, action=action, error=f"Control failed: {e}")

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        access_token: str | None = self._get_access_token()
        if not access_token:
            return {"error": "NEST_ACCESS_TOKEN not configured"}

        try:
            import httpx
        except ImportError:
            return {"error": "httpx is not installed"}

        # cloud_id passed directly as kwarg, or via a DiscoveredDevice object
        cloud_id: str = kwargs.get("cloud_id", "")
        if not cloud_id:
            device = kwargs.get("device")
            if device and hasattr(device, "cloud_id"):
                cloud_id = device.cloud_id or ""
        if not cloud_id:
            return {"error": "No cloud device ID available"}

        headers: dict[str, str] = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{SDM_API_BASE}/{cloud_id}",
                    headers=headers,
                )
                if resp.status_code == 401:
                    return {"error": "Nest access token expired — re-authenticate"}
                if resp.status_code != 200:
                    return {"error": f"Nest API returned {resp.status_code}"}

                data: dict[str, Any] = resp.json()
                traits: dict[str, Any] = data.get("traits", {})
                device_type: str = data.get("type", "")
                state: dict[str, Any] = {}
                temp_unit: str = self._get_temp_unit()

                # Thermostat traits
                mode_trait: dict[str, Any] = traits.get(
                    "sdm.devices.traits.ThermostatMode", {}
                )
                if mode_trait:
                    current_mode: str = mode_trait.get("mode", "OFF")
                    state["state"] = "off" if current_mode == "OFF" else "on"
                    state["mode"] = current_mode
                    available_modes: list[str] = mode_trait.get("availableModes", [])
                    state["available_modes"] = available_modes

                temp_trait: dict[str, Any] = traits.get(
                    "sdm.devices.traits.Temperature", {}
                )
                if temp_trait:
                    ambient_c: float = temp_trait.get("ambientTemperatureCelsius", 0)
                    if temp_unit == "F":
                        state["current_temperature"] = round(_c_to_f(ambient_c), 1)
                    else:
                        state["current_temperature"] = round(ambient_c, 1)
                    state["temperature_unit"] = temp_unit

                setpoint_trait: dict[str, Any] = traits.get(
                    "sdm.devices.traits.ThermostatTemperatureSetpoint", {}
                )
                if setpoint_trait:
                    if "heatCelsius" in setpoint_trait:
                        heat_c: float = setpoint_trait["heatCelsius"]
                        if temp_unit == "F":
                            state["heat_setpoint"] = round(_c_to_f(heat_c), 1)
                        else:
                            state["heat_setpoint"] = round(heat_c, 1)
                    if "coolCelsius" in setpoint_trait:
                        cool_c: float = setpoint_trait["coolCelsius"]
                        if temp_unit == "F":
                            state["cool_setpoint"] = round(_c_to_f(cool_c), 1)
                        else:
                            state["cool_setpoint"] = round(cool_c, 1)
                    # Set target_temperature from the active setpoint
                    current_mode_lower: str = state.get("mode", "").lower()
                    if current_mode_lower == "cool" and "cool_setpoint" in state:
                        state["target_temperature"] = state["cool_setpoint"]
                    elif "heat_setpoint" in state:
                        state["target_temperature"] = state["heat_setpoint"]

                humidity_trait: dict[str, Any] = traits.get(
                    "sdm.devices.traits.Humidity", {}
                )
                if humidity_trait:
                    state["humidity"] = humidity_trait.get("ambientHumidityPercent")

                hvac_trait: dict[str, Any] = traits.get(
                    "sdm.devices.traits.ThermostatHvac", {}
                )
                if hvac_trait:
                    state["hvac_status"] = hvac_trait.get("status", "OFF")

                # Camera traits
                connectivity_trait: dict[str, Any] = traits.get(
                    "sdm.devices.traits.CameraLiveStream", {}
                )
                if connectivity_trait:
                    state["has_stream"] = True

                cam_connectivity: dict[str, Any] = traits.get(
                    "sdm.devices.traits.Connectivity", {}
                )
                if cam_connectivity:
                    status_val: str = cam_connectivity.get("status", "OFFLINE")
                    state["state"] = "on" if status_val == "ONLINE" else "off"
                    state["connectivity"] = status_val

                return state

        except Exception as e:
            return {"error": f"Failed to get state: {e}"}
