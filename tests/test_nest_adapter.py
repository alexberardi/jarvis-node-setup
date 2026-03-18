"""Tests for the Google Nest SDM adapter."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from device_families.base import DeviceControlResult, DiscoveredDevice
from device_families.nest_adapter import (
    NestProtocol,
    _c_to_f,
    _extract_device_name,
    _f_to_c,
    _slugify,
)


@pytest.fixture
def nest() -> NestProtocol:
    return NestProtocol()


# Sample SDM API responses
THERMOSTAT_DEVICE = {
    "name": "enterprises/proj-123/devices/thermo-001",
    "type": "sdm.devices.types.THERMOSTAT",
    "traits": {
        "sdm.devices.traits.Info": {"customName": "Living Room"},
        "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
        "sdm.devices.traits.Temperature": {"ambientTemperatureCelsius": 22.2},
        "sdm.devices.traits.Humidity": {"ambientHumidityPercent": 45},
        "sdm.devices.traits.ThermostatHvac": {"status": "HEATING"},
        "sdm.devices.traits.ThermostatMode": {"mode": "HEAT"},
        "sdm.devices.traits.ThermostatTemperatureSetpoint": {"heatCelsius": 21.1},
    },
}

DOORBELL_DEVICE = {
    "name": "enterprises/proj-123/devices/door-001",
    "type": "sdm.devices.types.DOORBELL",
    "traits": {
        "sdm.devices.traits.Info": {"customName": "Front Door"},
        "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
    },
}

CAMERA_DEVICE = {
    "name": "enterprises/proj-123/devices/cam-001",
    "type": "sdm.devices.types.CAMERA",
    "traits": {
        "sdm.devices.traits.Connectivity": {"status": "OFFLINE"},
    },
}


# =============================================================================
# Protocol properties
# =============================================================================


class TestNestProtocolProperties:
    def test_protocol_name(self, nest: NestProtocol) -> None:
        assert nest.protocol_name == "nest"

    def test_friendly_name(self, nest: NestProtocol) -> None:
        assert nest.friendly_name == "Google Nest"

    def test_description(self, nest: NestProtocol) -> None:
        assert "SDM API" in nest.description

    def test_connection_type(self, nest: NestProtocol) -> None:
        assert nest.connection_type == "cloud"

    def test_supported_domains(self, nest: NestProtocol) -> None:
        assert nest.supported_domains == ["climate", "camera"]

    def test_required_secrets(self, nest: NestProtocol) -> None:
        secrets = nest.required_secrets
        keys = [s.key for s in secrets]
        assert "NEST_PROJECT_ID" in keys
        assert "NEST_ACCESS_TOKEN" in keys
        assert "NEST_REFRESH_TOKEN" in keys
        assert "NEST_CLIENT_ID" in keys
        assert "NEST_TEMP_UNIT" in keys

        # Only project ID is required
        required = [s for s in secrets if s.required]
        assert len(required) == 1
        assert required[0].key == "NEST_PROJECT_ID"

    def test_authentication_config(self, nest: NestProtocol) -> None:
        with patch(
            "device_families.nest_adapter._get_secret", return_value=None
        ):
            auth = nest.authentication
        assert auth.type == "oauth"
        assert auth.provider == "google_nest"
        assert auth.supports_pkce is True
        assert auth.requires_background_refresh is True
        assert auth.refresh_token_secret_key == "NEST_REFRESH_TOKEN"
        assert "sdm.service" in auth.scopes[0]
        assert auth.native_redirect_uri is not None
        assert auth.extra_authorize_params["access_type"] == "offline"
        # Without project ID, uses fallback URL
        assert "nestservices.google.com" in auth.authorize_url

    def test_authentication_config_with_project_id(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_PROJECT_ID": "proj-123",
                "NEST_CLIENT_ID": None,
            }.get(k)
            auth = nest.authentication
        assert "proj-123" in auth.authorize_url
        assert "nestservices.google.com/partnerconnections/proj-123/auth" in auth.authorize_url

    def test_supported_actions(self, nest: NestProtocol) -> None:
        actions = nest.supported_actions
        action_names = [a.button_action for a in actions]
        assert "set_temperature" in action_names
        assert "set_mode" in action_names


# =============================================================================
# Discovery
# =============================================================================


class TestNestDiscovery:
    @pytest.mark.asyncio
    async def test_discover_thermostat_and_doorbell(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "devices": [THERMOSTAT_DEVICE, DOORBELL_DEVICE],
        }
        mock_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_PROJECT_ID": "proj-123",
                "NEST_ACCESS_TOKEN": "tok-abc",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                devices = await nest.discover()

        assert len(devices) == 2

        thermo = devices[0]
        assert thermo.name == "Living Room"
        assert thermo.domain == "climate"
        assert thermo.manufacturer == "Google"
        assert thermo.model == "Thermostat"
        assert thermo.protocol == "nest"
        assert thermo.cloud_id == "enterprises/proj-123/devices/thermo-001"
        assert thermo.is_controllable is True
        assert thermo.device_class is None

        doorbell = devices[1]
        assert doorbell.name == "Front Door"
        assert doorbell.domain == "camera"
        assert doorbell.is_controllable is False
        assert doorbell.device_class == "doorbell"

    @pytest.mark.asyncio
    async def test_discover_camera_offline(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"devices": [CAMERA_DEVICE]}
        mock_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_PROJECT_ID": "proj-123",
                "NEST_ACCESS_TOKEN": "tok-abc",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                devices = await nest.discover()

        assert len(devices) == 1
        cam = devices[0]
        assert cam.domain == "camera"
        assert cam.model == "Camera"
        assert cam.is_controllable is False

    @pytest.mark.asyncio
    async def test_discover_missing_project_id(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret", return_value=None):
            devices = await nest.discover()
        assert devices == []

    @pytest.mark.asyncio
    async def test_discover_missing_token(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_PROJECT_ID": "proj-123",
            }.get(k)
            devices = await nest.discover()
        assert devices == []

    @pytest.mark.asyncio
    async def test_discover_api_error(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=mock_resp
        )

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_PROJECT_ID": "proj-123",
                "NEST_ACCESS_TOKEN": "tok-abc",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                devices = await nest.discover()

        assert devices == []

    @pytest.mark.asyncio
    async def test_discover_401_unauthorized(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=mock_resp
        )

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_PROJECT_ID": "proj-123",
                "NEST_ACCESS_TOKEN": "expired-tok",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                devices = await nest.discover()

        assert devices == []

    @pytest.mark.asyncio
    async def test_discover_no_custom_name_fallback(self, nest: NestProtocol) -> None:
        """Device without customName falls back to type-based name."""
        device = {
            "name": "enterprises/proj-123/devices/thermo-002",
            "type": "sdm.devices.types.THERMOSTAT",
            "traits": {
                "sdm.devices.traits.Info": {},
                "sdm.devices.traits.Connectivity": {"status": "ONLINE"},
            },
        }
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"devices": [device]}
        mock_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_PROJECT_ID": "proj-123",
                "NEST_ACCESS_TOKEN": "tok-abc",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                devices = await nest.discover()

        assert devices[0].name == "Thermostat"


# =============================================================================
# Control
# =============================================================================


class TestNestControl:
    @pytest.mark.asyncio
    async def test_set_temperature_fahrenheit(self, nest: NestProtocol) -> None:
        """Set temperature in F, converted to C for API."""
        # Mock get_current_mode
        mode_resp = MagicMock()
        mode_resp.json.return_value = {
            "traits": {"sdm.devices.traits.ThermostatMode": {"mode": "HEAT"}},
        }
        mode_resp.raise_for_status = MagicMock()

        # Mock executeCommand
        cmd_resp = MagicMock()
        cmd_resp.json.return_value = {}
        cmd_resp.raise_for_status = MagicMock()

        call_count = 0

        async def mock_request(method: str = "GET", url: str = "", **kw: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            return mode_resp if call_count == 1 else cmd_resp

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
                "NEST_TEMP_UNIT": None,  # Default F
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mode_resp)
                mock_client.post = AsyncMock(return_value=cmd_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await nest.control(
                    "", "set_temperature",
                    data={"temperature": 72},
                    cloud_id="enterprises/proj/devices/t1",
                    entity_id="climate.living_room",
                )

        assert result.success is True
        assert result.action == "set_temperature"

        # Verify the POST payload used heatCelsius
        post_call = mock_client.post.call_args
        payload = post_call.kwargs.get("json") or post_call[1].get("json")
        assert "heatCelsius" in payload["params"]
        # 72F ≈ 22.2C
        assert 22.0 <= payload["params"]["heatCelsius"] <= 22.3

    @pytest.mark.asyncio
    async def test_set_temperature_cool_mode(self, nest: NestProtocol) -> None:
        """In COOL mode, uses coolCelsius."""
        mode_resp = MagicMock()
        mode_resp.json.return_value = {
            "traits": {"sdm.devices.traits.ThermostatMode": {"mode": "COOL"}},
        }
        mode_resp.raise_for_status = MagicMock()

        cmd_resp = MagicMock()
        cmd_resp.json.return_value = {}
        cmd_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
                "NEST_TEMP_UNIT": None,
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mode_resp)
                mock_client.post = AsyncMock(return_value=cmd_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await nest.control(
                    "", "set_temperature",
                    data={"temperature": 75},
                    cloud_id="enterprises/proj/devices/t1",
                    entity_id="climate.living_room",
                )

        assert result.success is True
        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        assert "coolCelsius" in payload["params"]

    @pytest.mark.asyncio
    async def test_set_mode(self, nest: NestProtocol) -> None:
        cmd_resp = MagicMock()
        cmd_resp.json.return_value = {}
        cmd_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=cmd_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await nest.control(
                    "", "set_mode",
                    data={"mode": "cool"},
                    cloud_id="enterprises/proj/devices/t1",
                    entity_id="climate.living_room",
                )

        assert result.success is True
        payload = mock_client.post.call_args.kwargs.get("json") or mock_client.post.call_args[1]["json"]
        assert payload["params"]["mode"] == "COOL"

    @pytest.mark.asyncio
    async def test_set_mode_invalid(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            result = await nest.control(
                "", "set_mode",
                data={"mode": "turbo"},
                cloud_id="enterprises/proj/devices/t1",
                entity_id="climate.living_room",
            )

        assert result.success is False
        assert "Unsupported mode" in result.error

    @pytest.mark.asyncio
    async def test_get_stream(self, nest: NestProtocol) -> None:
        cmd_resp = MagicMock()
        cmd_resp.json.return_value = {
            "results": {
                "streamUrls": {"rtspUrl": "rtsp://example.com/stream"},
                "streamToken": "tok-123",
            }
        }
        cmd_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post = AsyncMock(return_value=cmd_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await nest.control(
                    "", "get_stream",
                    cloud_id="enterprises/proj/devices/cam-001",
                    entity_id="camera.front_door",
                )

        assert result.success is True
        assert result.action == "get_stream"

    @pytest.mark.asyncio
    async def test_unsupported_action(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            result = await nest.control(
                "", "turn_on",
                cloud_id="enterprises/proj/devices/t1",
                entity_id="climate.living_room",
            )

        assert result.success is False
        assert "Unsupported action" in result.error

    @pytest.mark.asyncio
    async def test_missing_token(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret", return_value=None):
            result = await nest.control(
                "", "set_temperature",
                data={"temperature": 72},
                cloud_id="enterprises/proj/devices/t1",
                entity_id="climate.living_room",
            )

        assert result.success is False
        assert "NEST_ACCESS_TOKEN" in result.error

    @pytest.mark.asyncio
    async def test_missing_cloud_id(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            result = await nest.control(
                "", "set_temperature",
                data={"temperature": 72},
                entity_id="climate.living_room",
            )

        assert result.success is False
        assert "cloud_id" in result.error

    @pytest.mark.asyncio
    async def test_set_temperature_missing_value(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            result = await nest.control(
                "", "set_temperature",
                data={},
                cloud_id="enterprises/proj/devices/t1",
                entity_id="climate.living_room",
            )

        assert result.success is False
        assert "temperature value is required" in result.error

    @pytest.mark.asyncio
    async def test_control_401_error(self, nest: NestProtocol) -> None:
        mode_resp = MagicMock()
        mode_resp.json.return_value = {
            "traits": {"sdm.devices.traits.ThermostatMode": {"mode": "HEAT"}},
        }
        mode_resp.raise_for_status = MagicMock()

        cmd_resp = MagicMock()
        cmd_resp.status_code = 401
        cmd_resp.text = "Unauthorized"
        cmd_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "401", request=MagicMock(), response=cmd_resp
        )

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
                "NEST_TEMP_UNIT": None,
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mode_resp)
                mock_client.post = AsyncMock(return_value=cmd_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await nest.control(
                    "", "set_temperature",
                    data={"temperature": 72},
                    cloud_id="enterprises/proj/devices/t1",
                    entity_id="climate.living_room",
                )

        assert result.success is False
        assert "401" in result.error


# =============================================================================
# Get state
# =============================================================================


class TestNestGetState:
    @pytest.mark.asyncio
    async def test_thermostat_state(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = THERMOSTAT_DEVICE
        mock_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                state = await nest.get_state(
                    "", cloud_id="enterprises/proj/devices/thermo-001"
                )

        assert state is not None
        assert state["state"] == "heating"
        assert state["mode"] == "HEAT"
        assert state["online"] is True
        assert state["current_temperature_c"] == 22.2
        assert state["current_temperature_f"] == round(_c_to_f(22.2), 0)
        assert state["target_temperature_c"] == 21.1
        assert state["target_temperature_f"] == round(_c_to_f(21.1), 0)
        assert state["humidity"] == 45

    @pytest.mark.asyncio
    async def test_camera_state(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = DOORBELL_DEVICE
        mock_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                state = await nest.get_state(
                    "", cloud_id="enterprises/proj/devices/door-001"
                )

        assert state == {"state": "online", "online": True}

    @pytest.mark.asyncio
    async def test_camera_offline(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.json.return_value = CAMERA_DEVICE
        mock_resp.raise_for_status = MagicMock()

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                state = await nest.get_state(
                    "", cloud_id="enterprises/proj/devices/cam-001"
                )

        assert state == {"state": "offline", "online": False}

    @pytest.mark.asyncio
    async def test_state_missing_token(self, nest: NestProtocol) -> None:
        with patch("device_families.nest_adapter._get_secret", return_value=None):
            state = await nest.get_state(
                "", cloud_id="enterprises/proj/devices/t1"
            )
        assert state is None

    @pytest.mark.asyncio
    async def test_state_api_error(self, nest: NestProtocol) -> None:
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Error"
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "500", request=MagicMock(), response=mock_resp
        )

        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_ACCESS_TOKEN": "tok",
            }.get(k)

            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_resp)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                state = await nest.get_state(
                    "", cloud_id="enterprises/proj/devices/t1"
                )

        assert state is None


# =============================================================================
# Store auth
# =============================================================================


class TestNestStoreAuth:
    def test_stores_tokens_and_clears_flag(self, nest: NestProtocol) -> None:
        with patch("services.secret_service.set_secret") as mock_set, \
             patch("services.command_auth_service.clear_auth_flag") as mock_clear:
            nest.store_auth_values({
                "access_token": "new-access",
                "refresh_token": "new-refresh",
            })

        mock_set.assert_any_call("NEST_ACCESS_TOKEN", "new-access", "integration")
        mock_set.assert_any_call("NEST_REFRESH_TOKEN", "new-refresh", "integration")
        mock_clear.assert_called_once_with("google_nest")

    def test_stores_only_access_token(self, nest: NestProtocol) -> None:
        with patch("services.secret_service.set_secret") as mock_set, \
             patch("services.command_auth_service.clear_auth_flag") as mock_clear:
            nest.store_auth_values({"access_token": "only-access"})

        mock_set.assert_called_once_with("NEST_ACCESS_TOKEN", "only-access", "integration")
        mock_clear.assert_called_once_with("google_nest")


# =============================================================================
# Temperature conversion + helpers
# =============================================================================


class TestNestTempConversion:
    def test_f_to_c(self) -> None:
        assert round(_f_to_c(32), 1) == 0.0
        assert round(_f_to_c(212), 1) == 100.0
        assert round(_f_to_c(72), 1) == 22.2

    def test_c_to_f(self) -> None:
        assert round(_c_to_f(0), 1) == 32.0
        assert round(_c_to_f(100), 1) == 212.0
        assert round(_c_to_f(22.2), 1) == 72.0

    def test_to_celsius_fahrenheit_default(self, nest: NestProtocol) -> None:
        """Default unit is F, so 72 → ~22.2C."""
        with patch("device_families.nest_adapter._get_secret", return_value=None):
            result = nest._to_celsius(72)
        assert round(result, 1) == 22.2

    def test_to_celsius_celsius_unit(self, nest: NestProtocol) -> None:
        """When unit is C, value passes through unchanged."""
        with patch("device_families.nest_adapter._get_secret") as mock_secret:
            mock_secret.side_effect = lambda k, *a: {
                "NEST_TEMP_UNIT": "C",
            }.get(k)
            result = nest._to_celsius(22.0)
        assert result == 22.0

    def test_slugify(self) -> None:
        assert _slugify("Living Room") == "living_room"
        assert _slugify("Front Door!") == "front_door"
        assert _slugify("  Kitchen  ") == "kitchen"

    def test_extract_device_name_custom(self) -> None:
        device = {
            "type": "sdm.devices.types.THERMOSTAT",
            "traits": {"sdm.devices.traits.Info": {"customName": "My Thermostat"}},
        }
        assert _extract_device_name(device) == "My Thermostat"

    def test_extract_device_name_fallback(self) -> None:
        device = {
            "type": "sdm.devices.types.DOORBELL",
            "traits": {"sdm.devices.traits.Info": {}},
        }
        assert _extract_device_name(device) == "Doorbell"

    def test_extract_device_name_unknown_type(self) -> None:
        device = {
            "type": "sdm.devices.types.UNKNOWN",
            "traits": {},
        }
        assert _extract_device_name(device) == "Nest Device"
