"""Tests for HomeAssistantDeviceManager.

Tests property values, required secrets, authentication config,
and collect_devices mapping from HA entities to DeviceManagerDevice.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.ijarvis_device_manager import DeviceManagerDevice
from device_managers.home_assistant_manager import HomeAssistantDeviceManager


@pytest.fixture
def manager() -> HomeAssistantDeviceManager:
    return HomeAssistantDeviceManager()


# =============================================================================
# Property tests
# =============================================================================


class TestHomeAssistantManagerProperties:
    def test_name(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.name == "home_assistant"

    def test_friendly_name(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.friendly_name == "Home Assistant"

    def test_can_edit_devices_is_false(self, manager: HomeAssistantDeviceManager) -> None:
        """HA is source of truth, so mobile should NOT show edit UI."""
        assert manager.can_edit_devices is False

    def test_description(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.description != ""


# =============================================================================
# Secrets tests
# =============================================================================


class TestRequiredSecrets:
    def test_requires_two_secrets(self, manager: HomeAssistantDeviceManager) -> None:
        secrets = manager.required_secrets
        assert len(secrets) == 2

    def test_requires_rest_url(self, manager: HomeAssistantDeviceManager) -> None:
        keys = [s.key for s in manager.required_secrets]
        assert "HOME_ASSISTANT_REST_URL" in keys

    def test_requires_api_key(self, manager: HomeAssistantDeviceManager) -> None:
        keys = [s.key for s in manager.required_secrets]
        assert "HOME_ASSISTANT_API_KEY" in keys

    def test_rest_url_not_sensitive(self, manager: HomeAssistantDeviceManager) -> None:
        """REST URL is not sensitive (included in settings snapshots)."""
        url_secret = next(s for s in manager.required_secrets if s.key == "HOME_ASSISTANT_REST_URL")
        assert url_secret.is_sensitive is False

    def test_api_key_is_sensitive(self, manager: HomeAssistantDeviceManager) -> None:
        """API key is sensitive (excluded from settings snapshots)."""
        key_secret = next(s for s in manager.required_secrets if s.key == "HOME_ASSISTANT_API_KEY")
        assert key_secret.is_sensitive is True

    def test_all_secrets_are_integration_scope(self, manager: HomeAssistantDeviceManager) -> None:
        for secret in manager.required_secrets:
            assert secret.scope == "integration"

    def test_secrets_have_friendly_names(self, manager: HomeAssistantDeviceManager) -> None:
        for secret in manager.required_secrets:
            assert secret.friendly_name is not None


# =============================================================================
# Authentication config tests
# =============================================================================


class TestAuthentication:
    def test_authentication_not_none(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.authentication is not None

    def test_auth_type_oauth(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.authentication.type == "oauth"

    def test_auth_provider(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.authentication.provider == "home_assistant"

    def test_auth_friendly_name(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.authentication.friendly_name == "Home Assistant"

    def test_auth_client_id(self, manager: HomeAssistantDeviceManager) -> None:
        assert manager.authentication.client_id == "http://jarvis-node-mobile"

    def test_auth_keys(self, manager: HomeAssistantDeviceManager) -> None:
        assert "access_token" in manager.authentication.keys

    def test_auth_local_discovery(self, manager: HomeAssistantDeviceManager) -> None:
        """HA uses local discovery (paths, not full URLs)."""
        auth = manager.authentication
        assert auth.authorize_path == "/auth/authorize"
        assert auth.exchange_path == "/auth/token"
        assert auth.discovery_port == 8123
        assert auth.discovery_probe_path == "/api/"

    def test_auth_no_redirect_uri_in_exchange(self, manager: HomeAssistantDeviceManager) -> None:
        """HA does not want redirect_uri in the token exchange."""
        assert manager.authentication.send_redirect_uri_in_exchange is False


# =============================================================================
# collect_devices tests
# =============================================================================


class TestCollectDevices:
    @pytest.mark.asyncio
    async def test_empty_context_returns_empty(self, manager: HomeAssistantDeviceManager) -> None:
        """No devices in HA context returns empty list."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {"devices": []}

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()
            assert result == []
            mock_service.connect_and_fetch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_maps_single_device_entity(self, manager: HomeAssistantDeviceManager) -> None:
        """A single HA device with one entity is mapped correctly."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {
            "devices": [
                {
                    "name": "Kitchen Light",
                    "area": "Kitchen",
                    "manufacturer": "Philips",
                    "model": "Hue Bulb",
                    "entities": [
                        {
                            "entity_id": "light.kitchen",
                            "name": "Kitchen Light",
                            "state": "on",
                        }
                    ],
                }
            ]
        }

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()

            assert len(result) == 1
            dev = result[0]
            assert isinstance(dev, DeviceManagerDevice)
            assert dev.name == "Kitchen Light"
            assert dev.domain == "light"
            assert dev.entity_id == "light.kitchen"
            assert dev.manufacturer == "Philips"
            assert dev.model == "Hue Bulb"
            assert dev.protocol == "home_assistant"
            assert dev.source == "home_assistant"
            assert dev.area == "Kitchen"
            assert dev.state == "on"
            assert dev.is_controllable is True

    @pytest.mark.asyncio
    async def test_maps_multi_entity_device(self, manager: HomeAssistantDeviceManager) -> None:
        """A device with multiple entities creates multiple DeviceManagerDevices."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {
            "devices": [
                {
                    "name": "Thermostat",
                    "area": "Hallway",
                    "manufacturer": "Nest",
                    "model": "Learning",
                    "entities": [
                        {"entity_id": "climate.thermostat", "name": "Thermostat", "state": "heat"},
                        {"entity_id": "sensor.thermostat_temp", "name": "Temperature", "state": "72"},
                    ],
                }
            ]
        }

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()
            assert len(result) == 2

            domains = {d.domain for d in result}
            assert "climate" in domains
            assert "sensor" in domains

    @pytest.mark.asyncio
    async def test_domain_extracted_from_entity_id(self, manager: HomeAssistantDeviceManager) -> None:
        """Domain is extracted from the entity_id prefix."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {
            "devices": [
                {
                    "name": "Lock",
                    "entities": [
                        {"entity_id": "lock.front_door", "state": "locked"},
                    ],
                }
            ]
        }

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()
            assert result[0].domain == "lock"

    @pytest.mark.asyncio
    async def test_entity_name_falls_back_to_device_name(self, manager: HomeAssistantDeviceManager) -> None:
        """When entity has no name, device name is used."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {
            "devices": [
                {
                    "name": "Garage Door",
                    "entities": [
                        {"entity_id": "cover.garage", "state": "closed"},
                    ],
                }
            ]
        }

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()
            assert result[0].name == "Garage Door"

    @pytest.mark.asyncio
    async def test_skips_entities_without_entity_id(self, manager: HomeAssistantDeviceManager) -> None:
        """Entities without entity_id are skipped."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {
            "devices": [
                {
                    "name": "Bad Device",
                    "entities": [
                        {"entity_id": "", "state": "on"},
                        {"state": "off"},
                    ],
                }
            ]
        }

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()
            assert result == []

    @pytest.mark.asyncio
    async def test_connection_failure_raises(self, manager: HomeAssistantDeviceManager) -> None:
        """Connection failure to HA raises an exception."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock(
            side_effect=ConnectionError("Cannot connect to HA")
        )

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            with pytest.raises(ConnectionError, match="Cannot connect to HA"):
                await manager.collect_devices()

    @pytest.mark.asyncio
    async def test_device_with_no_area(self, manager: HomeAssistantDeviceManager) -> None:
        """Device without an area maps area to None."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {
            "devices": [
                {
                    "name": "Orphan Light",
                    "entities": [
                        {"entity_id": "light.orphan", "state": "off"},
                    ],
                }
            ]
        }

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()
            assert result[0].area is None

    @pytest.mark.asyncio
    async def test_device_name_defaults_to_unknown(self, manager: HomeAssistantDeviceManager) -> None:
        """Device with no name defaults to 'Unknown'."""
        mock_service = MagicMock()
        mock_service.connect_and_fetch = AsyncMock()
        mock_service.get_context_data.return_value = {
            "devices": [
                {
                    "entities": [
                        {"entity_id": "light.unnamed"},
                    ],
                }
            ]
        }

        with patch(
            "services.home_assistant_service.HomeAssistantService",
            return_value=mock_service,
        ):
            result = await manager.collect_devices()
            assert result[0].name == "Unknown"
