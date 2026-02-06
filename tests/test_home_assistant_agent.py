"""
Unit tests for HomeAssistantAgent.

Tests WebSocket protocol handling, area fallback logic, and context data generation.
Uses mocks since actual HA server is not available in tests.
"""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agents.home_assistant_agent import (
    COMMON_ROOM_NAMES,
    HomeAssistantAgent,
    REFRESH_INTERVAL_SECONDS,
)


@pytest.fixture
def agent():
    """Create a HomeAssistantAgent instance"""
    return HomeAssistantAgent()


class TestHomeAssistantAgentProperties:
    """Test agent properties"""

    def test_name(self, agent):
        """Agent has correct name"""
        assert agent.name == "home_assistant"

    def test_description(self, agent):
        """Agent has description"""
        assert "home assistant" in agent.description.lower()

    def test_schedule(self, agent):
        """Agent schedule is configured correctly"""
        schedule = agent.schedule
        assert schedule.interval_seconds == REFRESH_INTERVAL_SECONDS
        assert schedule.run_on_startup is True

    def test_required_secrets(self, agent):
        """Agent requires HA secrets"""
        secrets = agent.required_secrets
        secret_keys = [s.key for s in secrets]

        assert "HOME_ASSISTANT_WS_URL" in secret_keys
        assert "HOME_ASSISTANT_API_KEY" in secret_keys

    def test_include_in_context(self, agent):
        """Agent includes data in context"""
        assert agent.include_in_context is True


class TestRunWithMissingSecrets:
    """Test run() behavior with missing secrets"""

    @pytest.mark.asyncio
    async def test_run_skips_without_url(self, agent):
        """run() skips if WS URL is missing"""
        with patch("agents.home_assistant_agent.get_secret_value") as mock_get:
            mock_get.side_effect = lambda key, scope: None

            await agent.run()

            assert agent._last_error is not None
            assert "missing" in agent._last_error.lower()

    @pytest.mark.asyncio
    async def test_run_skips_without_api_key(self, agent):
        """run() skips if API key is missing"""
        with patch("agents.home_assistant_agent.get_secret_value") as mock_get:
            # Only URL is set
            mock_get.side_effect = lambda key, scope: (
                "ws://localhost:8123/api/websocket"
                if key == "HOME_ASSISTANT_WS_URL"
                else None
            )

            await agent.run()

            assert agent._last_error is not None


class TestWebSocketProtocol:
    """Test WebSocket authentication and commands"""

    @pytest.mark.asyncio
    async def test_authenticate_success(self, agent):
        """Successful authentication flow"""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "auth_required", "ha_version": "2024.1.0"}),
                json.dumps({"type": "auth_ok", "ha_version": "2024.1.0"}),
            ]
        )

        await agent._authenticate(mock_ws, "test_token")

        # Should have sent auth message
        mock_ws.send.assert_called_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["type"] == "auth"
        assert sent["access_token"] == "test_token"

    @pytest.mark.asyncio
    async def test_authenticate_invalid(self, agent):
        """Authentication failure raises exception"""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            side_effect=[
                json.dumps({"type": "auth_required"}),
                json.dumps({"type": "auth_invalid", "message": "Invalid token"}),
            ]
        )

        with pytest.raises(ValueError) as exc:
            await agent._authenticate(mock_ws, "bad_token")

        assert "failed" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_send_command_success(self, agent):
        """send_command returns result on success"""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"id": 1, "success": True, "result": [{"name": "Living Room"}]}
            )
        )

        agent._message_id = 0
        result = await agent._send_command(mock_ws, "config/area_registry/list")

        assert result == [{"name": "Living Room"}]
        mock_ws.send.assert_called_once()
        sent = json.loads(mock_ws.send.call_args[0][0])
        assert sent["id"] == 1
        assert sent["type"] == "config/area_registry/list"

    @pytest.mark.asyncio
    async def test_send_command_failure(self, agent):
        """send_command raises on error response"""
        mock_ws = AsyncMock()
        mock_ws.recv = AsyncMock(
            return_value=json.dumps(
                {"id": 1, "success": False, "error": {"message": "Not found"}}
            )
        )

        agent._message_id = 0

        with pytest.raises(ValueError) as exc:
            await agent._send_command(mock_ws, "nonexistent/command")

        assert "failed" in str(exc.value).lower()


class TestGetContextData:
    """Test get_context_data method"""

    def test_empty_data(self, agent):
        """get_context_data works with no data"""
        result = agent.get_context_data()

        assert result["devices"] == []
        assert result["areas"] == []
        assert result["device_count"] == 0
        assert result["last_refresh"] is None
        assert result["last_error"] is None

    def test_with_areas(self, agent):
        """get_context_data includes areas"""
        agent._areas = [
            {"area_id": "area1", "name": "Living Room"},
            {"area_id": "area2", "name": "Kitchen"},
        ]

        result = agent.get_context_data()

        assert result["areas"] == ["Living Room", "Kitchen"]

    def test_with_devices_and_entities(self, agent):
        """get_context_data builds device list with entities"""
        agent._areas = [{"area_id": "area1", "name": "Living Room"}]
        agent._devices = [
            {
                "id": "device1",
                "name": "Desk Lamp",
                "area_id": "area1",
                "manufacturer": "Philips",
                "model": "Hue",
            }
        ]
        agent._entities = [
            {
                "entity_id": "light.desk_lamp",
                "device_id": "device1",
                "name": "Desk Lamp",
                "platform": "hue",
            }
        ]
        agent._states = {
            "light.desk_lamp": {
                "entity_id": "light.desk_lamp",
                "state": "on",
                "attributes": {"brightness": 255},
            }
        }

        result = agent.get_context_data()

        assert len(result["devices"]) == 1
        device = result["devices"][0]
        assert device["name"] == "Desk Lamp"
        assert device["area"] == "Living Room"
        assert len(device["entities"]) == 1

        entity = device["entities"][0]
        assert entity["entity_id"] == "light.desk_lamp"
        assert entity["state"] == "on"
        assert entity["attributes"]["brightness"] == 255

    def test_with_last_error(self, agent):
        """get_context_data includes last error"""
        agent._last_error = "Connection timeout"

        result = agent.get_context_data()

        assert result["last_error"] == "Connection timeout"

    def test_with_last_refresh(self, agent):
        """get_context_data includes last refresh timestamp"""
        now = datetime.now(timezone.utc)
        agent._last_refresh = now

        result = agent.get_context_data()

        assert result["last_refresh"] == now.isoformat()


class TestAreaInference:
    """Test area inference from entity names"""

    def test_infer_area_living_room(self, agent):
        """Infers 'Living Room' from entity name"""
        result = agent._infer_area_from_name("Living Room Light")

        assert result == "Living Room"

    def test_infer_area_kitchen(self, agent):
        """Infers 'Kitchen' from entity name"""
        result = agent._infer_area_from_name("Kitchen Ceiling Fan")

        assert result == "Kitchen"

    def test_infer_area_case_insensitive(self, agent):
        """Area inference is case-insensitive"""
        result = agent._infer_area_from_name("BEDROOM lamp")

        assert result == "Bedroom"

    def test_infer_area_no_match(self, agent):
        """Returns None when no room name found"""
        result = agent._infer_area_from_name("Smart Switch")

        assert result is None

    def test_infer_area_empty_name(self, agent):
        """Returns None for empty name"""
        result = agent._infer_area_from_name("")

        assert result is None

    def test_infer_area_none_name(self, agent):
        """Returns None for None name"""
        result = agent._infer_area_from_name(None)

        assert result is None

    def test_common_room_names_coverage(self):
        """All common room names are recognized"""
        # Verify the constant exists and has expected values
        assert "living room" in COMMON_ROOM_NAMES
        assert "bedroom" in COMMON_ROOM_NAMES
        assert "kitchen" in COMMON_ROOM_NAMES
        assert "bathroom" in COMMON_ROOM_NAMES
        assert "office" in COMMON_ROOM_NAMES


class TestFullFetchFlow:
    """Test the full data fetch flow"""

    @pytest.mark.asyncio
    async def test_fetch_all_data_success(self, agent):
        """Full fetch flow with mocked WebSocket"""
        # Mock the websockets module
        mock_ws = AsyncMock()

        # Auth flow
        auth_recv_calls = [
            json.dumps({"type": "auth_required", "ha_version": "2024.1.0"}),
            json.dumps({"type": "auth_ok", "ha_version": "2024.1.0"}),
        ]

        # Command responses (areas, devices, entities, states)
        command_responses = [
            json.dumps(
                {
                    "id": 1,
                    "success": True,
                    "result": [{"area_id": "area1", "name": "Living Room"}],
                }
            ),
            json.dumps(
                {
                    "id": 2,
                    "success": True,
                    "result": [
                        {
                            "id": "device1",
                            "name": "Light",
                            "area_id": "area1",
                            "manufacturer": "Philips",
                            "model": "Hue",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "id": 3,
                    "success": True,
                    "result": [
                        {
                            "entity_id": "light.living_room",
                            "device_id": "device1",
                            "name": "Living Room Light",
                            "platform": "hue",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "id": 4,
                    "success": True,
                    "result": [
                        {
                            "entity_id": "light.living_room",
                            "state": "on",
                            "attributes": {"brightness": 200},
                        }
                    ],
                }
            ),
        ]

        mock_ws.recv = AsyncMock(side_effect=auth_recv_calls + command_responses)
        mock_ws.send = AsyncMock()

        # Create a mock websockets module
        mock_websockets = MagicMock()
        mock_websockets.connect = MagicMock(return_value=mock_ws)
        mock_ws.__aenter__ = AsyncMock(return_value=mock_ws)
        mock_ws.__aexit__ = AsyncMock(return_value=None)

        with patch.dict("sys.modules", {"websockets": mock_websockets}):
            await agent._fetch_all_data(
                "ws://localhost:8123/api/websocket", "test_token"
            )

        assert len(agent._areas) == 1
        assert len(agent._devices) == 1
        assert len(agent._entities) == 1
        assert len(agent._states) == 1


class TestBuildLightControls:
    """Test the _build_light_controls method for LLM context."""

    def test_empty_states(self, agent):
        """Returns empty dict when no states."""
        result = agent._build_light_controls()
        assert result == {}

    def test_ignores_non_light_entities(self, agent):
        """Only includes light.* entities."""
        agent._states = {
            "switch.garage": {"state": "off", "attributes": {}},
            "sensor.temperature": {"state": "72", "attributes": {}},
        }

        result = agent._build_light_controls()
        assert result == {}

    def test_ignores_non_room_groups(self, agent):
        """Ignores individual lights (no hue_type=room)."""
        agent._states = {
            "light.desk_lamp": {
                "state": "on",
                "attributes": {"friendly_name": "Desk Lamp"},
            }
        }

        result = agent._build_light_controls()
        assert result == {}

    def test_includes_hue_room_groups(self, agent):
        """Includes lights with hue_type=room."""
        agent._states = {
            "light.basement": {
                "state": "off",
                "attributes": {
                    "friendly_name": "Basement",
                    "hue_type": "room",
                },
            }
        }

        result = agent._build_light_controls()

        assert "Basement" in result
        assert result["Basement"]["entity_id"] == "light.basement"
        assert result["Basement"]["state"] == "off"
        assert result["Basement"]["type"] == "room_group"

    def test_includes_is_hue_group(self, agent):
        """Includes lights with is_hue_group=True."""
        agent._states = {
            "light.my_office": {
                "state": "on",
                "attributes": {
                    "friendly_name": "My office",
                    "is_hue_group": True,
                },
            }
        }

        result = agent._build_light_controls()

        assert "My office" in result
        assert result["My office"]["entity_id"] == "light.my_office"
        assert result["My office"]["state"] == "on"

    def test_multiple_room_groups(self, agent):
        """Handles multiple room groups."""
        agent._states = {
            "light.basement": {
                "state": "off",
                "attributes": {
                    "friendly_name": "Basement",
                    "hue_type": "room",
                },
            },
            "light.my_office": {
                "state": "on",
                "attributes": {
                    "friendly_name": "My office",
                    "hue_type": "room",
                },
            },
            "light.middle_bathroom": {
                "state": "off",
                "attributes": {
                    "friendly_name": "Middle Bathroom",
                    "hue_type": "room",
                },
            },
        }

        result = agent._build_light_controls()

        assert len(result) == 3
        assert "Basement" in result
        assert "My office" in result
        assert "Middle Bathroom" in result

    def test_uses_entity_id_as_fallback_name(self, agent):
        """Falls back to entity_id if no friendly_name."""
        agent._states = {
            "light.unknown_room": {
                "state": "off",
                "attributes": {
                    "hue_type": "room",
                },
            }
        }

        result = agent._build_light_controls()

        # Should use entity_id as the key
        assert "light.unknown_room" in result

    def test_context_data_includes_light_controls(self, agent):
        """get_context_data includes light_controls field."""
        agent._states = {
            "light.basement": {
                "state": "off",
                "attributes": {
                    "friendly_name": "Basement",
                    "hue_type": "room",
                },
            },
        }

        context = agent.get_context_data()

        assert "light_controls" in context
        assert "Basement" in context["light_controls"]


class TestBuildDeviceControls:
    """Test the _build_device_controls method for LLM context."""

    def test_empty_states(self, agent):
        """Returns empty dict when no states."""
        result = agent._build_device_controls()
        assert result == {}

    def test_ignores_sensor_domains(self, agent):
        """Excludes sensor-only domains."""
        agent._states = {
            "sensor.temperature": {"state": "72", "attributes": {}},
            "binary_sensor.motion": {"state": "off", "attributes": {}},
            "weather.home": {"state": "sunny", "attributes": {}},
        }

        result = agent._build_device_controls()
        assert result == {}

    def test_includes_controllable_domains(self, agent):
        """Includes controllable domains."""
        agent._states = {
            "cover.garage_door": {
                "state": "closed",
                "attributes": {"friendly_name": "Garage Door", "current_position": 0},
            },
            "lock.front_door": {
                "state": "locked",
                "attributes": {"friendly_name": "Front Door"},
            },
            "climate.thermostat": {
                "state": "heat",
                "attributes": {
                    "friendly_name": "Thermostat",
                    "current_temperature": 68,
                    "temperature": 72,
                },
            },
        }

        result = agent._build_device_controls()

        assert "cover" in result
        assert "lock" in result
        assert "climate" in result

    def test_groups_by_domain(self, agent):
        """Groups devices by domain."""
        agent._states = {
            "cover.garage_door": {
                "state": "closed",
                "attributes": {"friendly_name": "Garage Door"},
            },
            "cover.blinds": {
                "state": "open",
                "attributes": {"friendly_name": "Blinds"},
            },
        }

        result = agent._build_device_controls()

        assert len(result["cover"]) == 2

    def test_includes_device_info(self, agent):
        """Device info includes entity_id, name, and state."""
        agent._states = {
            "cover.garage_door": {
                "state": "closed",
                "attributes": {"friendly_name": "Garage Door"},
            },
        }

        result = agent._build_device_controls()
        device = result["cover"][0]

        assert device["entity_id"] == "cover.garage_door"
        assert device["name"] == "Garage Door"
        assert device["state"] == "closed"

    def test_includes_climate_attributes(self, agent):
        """Climate devices include temperature attributes."""
        agent._states = {
            "climate.thermostat": {
                "state": "heat",
                "attributes": {
                    "friendly_name": "Thermostat",
                    "current_temperature": 68,
                    "temperature": 72,
                    "hvac_modes": ["heat", "cool", "off"],
                },
            },
        }

        result = agent._build_device_controls()
        device = result["climate"][0]

        assert device["current_temperature"] == 68
        assert device["target_temperature"] == 72
        assert device["hvac_modes"] == ["heat", "cool", "off"]

    def test_includes_cover_position(self, agent):
        """Cover devices include position."""
        agent._states = {
            "cover.blinds": {
                "state": "open",
                "attributes": {
                    "friendly_name": "Blinds",
                    "current_position": 50,
                },
            },
        }

        result = agent._build_device_controls()
        device = result["cover"][0]

        assert device["current_position"] == 50

    def test_context_data_includes_device_controls(self, agent):
        """get_context_data includes device_controls field."""
        agent._states = {
            "cover.garage_door": {
                "state": "closed",
                "attributes": {"friendly_name": "Garage Door"},
            },
        }

        context = agent.get_context_data()

        assert "device_controls" in context
        assert "cover" in context["device_controls"]
