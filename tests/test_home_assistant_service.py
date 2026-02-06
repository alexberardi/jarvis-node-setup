"""
Unit tests for HomeAssistantService.

Tests REST API client functionality with mocked HTTP responses.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from services.home_assistant_service import (
    DOMAIN_ACTIONS,
    HomeAssistantService,
    LightAction,
    ServiceCallResult,
    EntityStateResult,
    get_domain_from_entity_id,
    get_actions_for_domain,
    get_action_display_name,
)


@pytest.fixture
def mock_secrets():
    """Mock secret values for HA connection."""
    with patch("services.home_assistant_service.get_secret_value") as mock:
        mock.side_effect = lambda key, scope: {
            "HOME_ASSISTANT_REST_URL": "http://localhost:8123",
            "HOME_ASSISTANT_API_KEY": "test_token_123",
        }.get(key)
        yield mock


@pytest.fixture
def service(mock_secrets):
    """Create a HomeAssistantService with mocked secrets."""
    return HomeAssistantService()


class TestHomeAssistantServiceInit:
    """Test service initialization."""

    def test_init_with_secrets(self, mock_secrets):
        """Service initializes with secrets."""
        service = HomeAssistantService()

        assert service._base_url == "http://localhost:8123"
        assert service._api_key == "test_token_123"

    def test_init_with_explicit_values(self):
        """Service accepts explicit URL and API key."""
        with patch("services.home_assistant_service.get_secret_value") as mock:
            mock.return_value = None

            service = HomeAssistantService(
                base_url="http://192.168.1.50:8123",
                api_key="explicit_token",
            )

            assert service._base_url == "http://192.168.1.50:8123"
            assert service._api_key == "explicit_token"

    def test_init_strips_trailing_slash(self):
        """Service strips trailing slash from base URL."""
        with patch("services.home_assistant_service.get_secret_value") as mock:
            mock.return_value = None

            service = HomeAssistantService(
                base_url="http://localhost:8123/",
                api_key="token",
            )

            assert service._base_url == "http://localhost:8123"

    def test_init_raises_without_url(self):
        """Service raises if URL is not available."""
        with patch("services.home_assistant_service.get_secret_value") as mock:
            mock.return_value = None

            with pytest.raises(ValueError) as exc:
                HomeAssistantService()

            assert "HOME_ASSISTANT_REST_URL" in str(exc.value)

    def test_init_raises_without_api_key(self):
        """Service raises if API key is not available."""
        with patch("services.home_assistant_service.get_secret_value") as mock:
            mock.side_effect = lambda key, scope: (
                "http://localhost:8123" if key == "HOME_ASSISTANT_REST_URL" else None
            )

            with pytest.raises(ValueError) as exc:
                HomeAssistantService()

            assert "HOME_ASSISTANT_API_KEY" in str(exc.value)


class TestGetHeaders:
    """Test header generation."""

    def test_headers_include_bearer_token(self, service):
        """Headers include Authorization with Bearer token."""
        headers = service._get_headers()

        assert headers["Authorization"] == "Bearer test_token_123"
        assert headers["Content-Type"] == "application/json"


class TestCallService:
    """Test the generic call_service method."""

    @pytest.mark.asyncio
    async def test_call_service_success(self, service):
        """Successful service call returns success result."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "[]"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.call_service(
                "light", "turn_on", "light.basement"
            )

            assert result.success is True
            assert result.entity_id == "light.basement"
            assert result.action == "light.turn_on"
            assert result.error is None

    @pytest.mark.asyncio
    async def test_call_service_with_data(self, service):
        """Service call includes additional data in payload."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "[]"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            await service.call_service(
                "light",
                "turn_on",
                "light.office",
                data={"brightness": 255},
            )

            # Verify the payload includes both entity_id and brightness
            call_args = mock_instance.post.call_args
            payload = call_args.kwargs["json"]
            assert payload["entity_id"] == "light.office"
            assert payload["brightness"] == 255

    @pytest.mark.asyncio
    async def test_call_service_http_error(self, service):
        """HTTP error returns failure result."""
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.call_service(
                "light", "turn_on", "light.basement"
            )

            assert result.success is False
            assert "401" in result.error
            assert "Unauthorized" in result.error

    @pytest.mark.asyncio
    async def test_call_service_timeout(self, service):
        """Timeout returns failure result."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(side_effect=httpx.TimeoutException("timeout"))
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.call_service(
                "light", "turn_on", "light.basement"
            )

            assert result.success is False
            assert "timed out" in result.error.lower()

    @pytest.mark.asyncio
    async def test_call_service_connection_error(self, service):
        """Connection error returns failure result."""
        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.call_service(
                "light", "turn_on", "light.basement"
            )

            assert result.success is False
            assert "connection" in result.error.lower()

    @pytest.mark.asyncio
    async def test_call_service_correct_url(self, service):
        """Service call uses correct URL pattern."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "[]"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            await service.call_service("switch", "toggle", "switch.garage")

            call_args = mock_instance.post.call_args
            url = call_args.args[0]
            assert url == "http://localhost:8123/api/services/switch/toggle"


class TestControlLight:
    """Test the control_light convenience method."""

    @pytest.mark.asyncio
    async def test_control_light_turn_on(self, service):
        """control_light with TURN_ON calls correct service."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "[]"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.control_light(
                "light.basement", LightAction.TURN_ON
            )

            assert result.success is True
            assert result.action == "light.turn_on"

            # Verify URL
            call_args = mock_instance.post.call_args
            url = call_args.args[0]
            assert "light/turn_on" in url

    @pytest.mark.asyncio
    async def test_control_light_turn_off(self, service):
        """control_light with TURN_OFF calls correct service."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "[]"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.control_light(
                "light.office", LightAction.TURN_OFF
            )

            assert result.success is True
            assert result.action == "light.turn_off"


class TestLightActionEnum:
    """Test LightAction enum values."""

    def test_turn_on_value(self):
        """TURN_ON has correct value."""
        assert LightAction.TURN_ON.value == "turn_on"

    def test_turn_off_value(self):
        """TURN_OFF has correct value."""
        assert LightAction.TURN_OFF.value == "turn_off"

    def test_from_string(self):
        """Can create from string value."""
        assert LightAction("turn_on") == LightAction.TURN_ON
        assert LightAction("turn_off") == LightAction.TURN_OFF

    def test_invalid_value_raises(self):
        """Invalid string raises ValueError."""
        with pytest.raises(ValueError):
            LightAction("toggle")


class TestServiceCallResult:
    """Test ServiceCallResult dataclass."""

    def test_success_result(self):
        """Success result has correct fields."""
        result = ServiceCallResult(
            success=True,
            entity_id="light.test",
            action="light.turn_on",
        )

        assert result.success is True
        assert result.entity_id == "light.test"
        assert result.action == "light.turn_on"
        assert result.error is None

    def test_failure_result(self):
        """Failure result includes error."""
        result = ServiceCallResult(
            success=False,
            entity_id="light.test",
            action="light.turn_on",
            error="Connection refused",
        )

        assert result.success is False
        assert result.error == "Connection refused"


class TestEntityStateResult:
    """Test EntityStateResult dataclass."""

    def test_success_result(self):
        """Success result has correct fields."""
        result = EntityStateResult(
            success=True,
            entity_id="cover.garage_door",
            state="closed",
            attributes={"current_position": 0},
            friendly_name="Garage Door",
        )

        assert result.success is True
        assert result.state == "closed"
        assert result.friendly_name == "Garage Door"

    def test_failure_result(self):
        """Failure result includes error."""
        result = EntityStateResult(
            success=False,
            entity_id="cover.nonexistent",
            error="Entity not found",
        )

        assert result.success is False
        assert result.error == "Entity not found"


class TestHelperFunctions:
    """Test helper functions."""

    def test_get_domain_from_entity_id_light(self):
        """Extracts domain from light entity."""
        assert get_domain_from_entity_id("light.basement") == "light"

    def test_get_domain_from_entity_id_cover(self):
        """Extracts domain from cover entity."""
        assert get_domain_from_entity_id("cover.garage_door") == "cover"

    def test_get_domain_from_entity_id_invalid(self):
        """Returns None for invalid format."""
        assert get_domain_from_entity_id("invalid") is None

    def test_get_actions_for_domain_cover(self):
        """Returns correct actions for cover domain."""
        actions = get_actions_for_domain("cover")
        assert "open_cover" in actions
        assert "close_cover" in actions
        assert "stop_cover" in actions

    def test_get_actions_for_domain_lock(self):
        """Returns correct actions for lock domain."""
        actions = get_actions_for_domain("lock")
        assert "lock" in actions
        assert "unlock" in actions

    def test_get_actions_for_domain_unknown(self):
        """Returns empty list for unknown domain."""
        actions = get_actions_for_domain("unknown_domain")
        assert actions == []

    def test_get_action_display_name(self):
        """Returns human-friendly display names."""
        assert get_action_display_name("open_cover") == "open"
        assert get_action_display_name("close_cover") == "close"
        assert get_action_display_name("turn_on") == "turn on"

    def test_get_action_display_name_unknown(self):
        """Falls back to replacing underscores for unknown actions."""
        assert get_action_display_name("unknown_action") == "unknown action"


class TestDomainActions:
    """Test DOMAIN_ACTIONS constant."""

    def test_has_common_domains(self):
        """Contains common controllable domains."""
        assert "light" in DOMAIN_ACTIONS
        assert "switch" in DOMAIN_ACTIONS
        assert "cover" in DOMAIN_ACTIONS
        assert "lock" in DOMAIN_ACTIONS
        assert "climate" in DOMAIN_ACTIONS
        assert "fan" in DOMAIN_ACTIONS

    def test_light_actions(self):
        """Light domain has expected actions."""
        actions = DOMAIN_ACTIONS["light"]
        assert "turn_on" in actions
        assert "turn_off" in actions
        assert "toggle" in actions

    def test_cover_actions(self):
        """Cover domain has expected actions."""
        actions = DOMAIN_ACTIONS["cover"]
        assert "open_cover" in actions
        assert "close_cover" in actions


class TestGetState:
    """Test the get_state method."""

    @pytest.mark.asyncio
    async def test_get_state_success(self, service):
        """Successful state query returns entity data."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "entity_id": "cover.garage_door",
            "state": "closed",
            "attributes": {
                "friendly_name": "Garage Door",
                "current_position": 0,
            },
        }

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_state("cover.garage_door")

            assert result.success is True
            assert result.state == "closed"
            assert result.friendly_name == "Garage Door"
            assert result.attributes["current_position"] == 0

    @pytest.mark.asyncio
    async def test_get_state_not_found(self, service):
        """Returns error when entity not found."""
        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.get = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.get_state("cover.nonexistent")

            assert result.success is False
            assert "not found" in result.error.lower()


class TestControlDevice:
    """Test the control_device method."""

    @pytest.mark.asyncio
    async def test_control_device_success(self, service):
        """Successful device control."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "[]"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.control_device(
                "cover.garage_door", "open_cover"
            )

            assert result.success is True
            assert result.action == "cover.open_cover"

    @pytest.mark.asyncio
    async def test_control_device_invalid_entity_id(self, service):
        """Returns error for invalid entity_id format."""
        result = await service.control_device("invalid_format", "turn_on")

        assert result.success is False
        assert "invalid entity_id" in result.error.lower()

    @pytest.mark.asyncio
    async def test_control_device_invalid_action(self, service):
        """Returns error for invalid action."""
        result = await service.control_device(
            "cover.garage_door", "turn_on"  # Not valid for cover
        )

        assert result.success is False
        assert "not valid" in result.error.lower()

    @pytest.mark.asyncio
    async def test_control_device_with_data(self, service):
        """Passes service data correctly."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "[]"

        with patch("httpx.AsyncClient") as mock_client:
            mock_instance = AsyncMock()
            mock_instance.post = AsyncMock(return_value=mock_response)
            mock_instance.__aenter__ = AsyncMock(return_value=mock_instance)
            mock_instance.__aexit__ = AsyncMock(return_value=None)
            mock_client.return_value = mock_instance

            result = await service.control_device(
                "climate.thermostat",
                "set_temperature",
                {"temperature": 72},
            )

            assert result.success is True

            # Verify data was passed
            call_args = mock_instance.post.call_args
            payload = call_args.kwargs["json"]
            assert payload["temperature"] == 72
