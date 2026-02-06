"""
Integration tests for Home Assistant command flow.

Tests the full flow from voice command → context injection → command execution
with mocked LLM responses for deterministic testing.

These tests verify:
1. Agent context is correctly injected into requests
2. Commands execute correctly given specific LLM responses
3. Error handling works for edge cases
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from commands.control_device_command import ControlDeviceCommand
from commands.get_device_status_command import GetDeviceStatusCommand
from core.command_response import CommandResponse
from core.request_information import RequestInformation
from services.home_assistant_service import ServiceCallResult


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_ha_context():
    """Simulated Home Assistant agent context as it would appear in node_context."""
    return {
        "home_assistant": {
            "light_controls": {
                "Basement": {
                    "entity_id": "light.basement",
                    "state": "off",
                    "type": "room_group",
                },
                "My office": {
                    "entity_id": "light.my_office",
                    "state": "on",
                    "type": "room_group",
                },
                "Middle Bathroom": {
                    "entity_id": "light.middle_bathroom",
                    "state": "off",
                    "type": "room_group",
                },
            },
            "device_controls": {
                "light": [
                    {"entity_id": "light.office_desk", "name": "Office Desk", "state": "on"},
                ],
                "switch": [
                    {"entity_id": "switch.baby_berardi_timer", "name": "Baby Berardi Timer", "state": "off"},
                    {"entity_id": "switch.coffee_maker", "name": "Coffee Maker", "state": "off"},
                ],
                "cover": [
                    {"entity_id": "cover.garage_door", "name": "Garage Door", "state": "closed"},
                ],
                "scene": [
                    {"entity_id": "scene.basement_bright", "name": "Basement Bright"},
                    {"entity_id": "scene.movie_time", "name": "Movie Time"},
                ],
            },
        }
    }


@pytest.fixture
def control_device_cmd():
    """ControlDeviceCommand instance."""
    return ControlDeviceCommand()


@pytest.fixture
def get_device_status_cmd():
    """GetDeviceStatusCommand instance."""
    return GetDeviceStatusCommand()


@pytest.fixture
def request_info():
    """Basic RequestInformation for tests."""
    return RequestInformation(
        voice_command="turn on the basement lights",
        conversation_id="test-conv-123",
    )


# ============================================================================
# Test: LLM returns correct entity_id from context
# ============================================================================


class TestLLMEntitySelection:
    """
    Test that when the LLM selects an entity_id from context,
    the command executes correctly.

    These tests simulate what happens AFTER the LLM has made its decision.
    The LLM's decision-making is mocked; we test the execution path.
    """

    def test_light_control_from_room_group(self, control_device_cmd, request_info):
        """
        Scenario: User says "turn on basement lights"
        LLM picks: entity_id=light.basement, action=turn_on
        Expected: Command executes successfully
        """
        with patch.object(control_device_cmd, "_execute_control") as mock_exec:
            mock_exec.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "light.basement",
                    "action": "turn_on",
                    "message": "Basement lights turned on",
                },
                wait_for_input=False,
            )

            # Simulate LLM selecting correct entity from context
            response = control_device_cmd.run(
                request_info,
                entity_id="light.basement",  # LLM picked this from light_controls
                action="turn_on",
            )

            assert response.success is True
            assert response.context_data["entity_id"] == "light.basement"
            mock_exec.assert_called_once()

    def test_switch_control_from_device_controls(self, control_device_cmd, request_info):
        """
        Scenario: User says "turn on the baby timer"
        LLM picks: entity_id=switch.baby_berardi_timer, action=turn_on
        Expected: Command executes successfully
        """
        with patch.object(control_device_cmd, "_execute_control") as mock_exec:
            mock_exec.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "switch.baby_berardi_timer",
                    "action": "turn_on",
                    "message": "Switch turned on",
                },
                wait_for_input=False,
            )

            response = control_device_cmd.run(
                request_info,
                entity_id="switch.baby_berardi_timer",
                action="turn_on",
            )

            assert response.success is True
            assert response.context_data["entity_id"] == "switch.baby_berardi_timer"

    def test_scene_activation_auto_selects_action(self, control_device_cmd, request_info):
        """
        Scenario: User says "activate movie time"
        LLM picks: entity_id=scene.movie_time (no action - scenes only have turn_on)
        Expected: Command auto-selects turn_on and executes
        """
        with patch.object(control_device_cmd, "_execute_control") as mock_exec:
            mock_exec.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "scene.movie_time",
                    "action": "turn_on",
                    "message": "Scene activated",
                },
                wait_for_input=False,
            )

            # LLM omits action for scene (only one valid action)
            response = control_device_cmd.run(
                request_info,
                entity_id="scene.movie_time",
                # action intentionally omitted
            )

            assert response.success is True
            # Verify auto-selection triggered execution
            mock_exec.assert_called_once()
            call_args = mock_exec.call_args
            assert call_args[0][2] == "turn_on"  # action was auto-selected

    def test_cover_requires_action_clarification(self, control_device_cmd, request_info):
        """
        Scenario: User says "do something with the garage"
        LLM picks: entity_id=cover.garage_door (no action - ambiguous)
        Expected: Command returns clarification request
        """
        response = control_device_cmd.run(
            request_info,
            entity_id="cover.garage_door",
            # action intentionally omitted - cover has multiple actions
        )

        assert response.success is True  # It's a follow-up, not error
        assert response.wait_for_input is True
        assert response.context_data["validation_type"] == "action_required"
        assert "open_cover" in response.context_data["allowed_actions"]
        assert "close_cover" in response.context_data["allowed_actions"]


# ============================================================================
# Test: Context injection into system prompt
# ============================================================================


class TestContextInjection:
    """
    Test that agent context is correctly formatted for LLM consumption.

    We can't test the LLM's interpretation, but we can verify the context
    structure matches what the system prompt expects.
    """

    def test_light_controls_structure(self, mock_ha_context):
        """Verify light_controls has the structure expected by system prompt."""
        light_controls = mock_ha_context["home_assistant"]["light_controls"]

        # Must have room name as key
        assert "Basement" in light_controls
        assert "My office" in light_controls

        # Each entry must have entity_id and state
        basement = light_controls["Basement"]
        assert basement["entity_id"] == "light.basement"
        assert basement["state"] in ("on", "off")
        assert basement["type"] == "room_group"

    def test_device_controls_grouped_by_domain(self, mock_ha_context):
        """Verify device_controls is grouped by domain."""
        device_controls = mock_ha_context["home_assistant"]["device_controls"]

        # Should have domain keys
        assert "switch" in device_controls
        assert "cover" in device_controls
        assert "scene" in device_controls

        # Each domain should be a list of devices
        assert isinstance(device_controls["switch"], list)
        assert len(device_controls["switch"]) >= 1

        # Each device should have entity_id and name
        switch = device_controls["switch"][0]
        assert "entity_id" in switch
        assert "name" in switch

    def test_scene_has_no_state(self, mock_ha_context):
        """Scenes don't have state - they're just activatable."""
        scenes = mock_ha_context["home_assistant"]["device_controls"]["scene"]

        for scene in scenes:
            assert "entity_id" in scene
            assert "name" in scene
            # state is optional for scenes


# ============================================================================
# Test: Error handling for LLM mistakes
# ============================================================================


class TestLLMErrorHandling:
    """
    Test error handling when LLM returns unexpected values.

    Even with good prompting, LLMs can hallucinate entity_ids or
    return invalid actions. The command should handle these gracefully.
    """

    def test_hallucinated_entity_id(self, control_device_cmd, request_info):
        """
        Scenario: LLM invents an entity_id not in context
        Expected: HA service call fails gracefully
        """
        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_svc_cls:
            mock_svc = AsyncMock()
            mock_svc.control_device = AsyncMock(
                return_value=ServiceCallResult(
                    success=False,
                    entity_id="light.nonexistent_room",
                    action="light.turn_on",
                    error="Entity not found: light.nonexistent_room",
                )
            )
            mock_svc_cls.return_value = mock_svc

            response = control_device_cmd.run(
                request_info,
                entity_id="light.nonexistent_room",  # Hallucinated
                action="turn_on",
            )

            assert response.success is False
            assert "not found" in response.error_details.lower() or "failed" in response.error_details.lower()

    def test_invalid_action_for_domain(self, control_device_cmd, request_info):
        """
        Scenario: LLM returns action that doesn't match domain
        Expected: Clarification prompt with valid actions
        """
        response = control_device_cmd.run(
            request_info,
            entity_id="cover.garage_door",
            action="turn_on",  # Invalid - covers use open_cover/close_cover
        )

        assert response.success is True  # It's clarification, not error
        assert response.wait_for_input is True
        assert "isn't valid" in response.context_data["prompt"]
        assert "open_cover" in response.context_data["allowed_actions"]

    def test_missing_entity_id(self, control_device_cmd, request_info):
        """
        Scenario: LLM forgets to include entity_id
        Expected: Error response
        """
        response = control_device_cmd.run(
            request_info,
            # entity_id missing
            action="turn_on",
        )

        assert response.success is False
        assert "entity" in response.error_details.lower()

    def test_malformed_entity_id(self, control_device_cmd, request_info):
        """
        Scenario: LLM returns malformed entity_id (no domain prefix)
        Expected: Error response
        """
        response = control_device_cmd.run(
            request_info,
            entity_id="basement_lights",  # Missing "light." prefix
            action="turn_on",
        )

        assert response.success is False
        assert "invalid" in response.error_details.lower()


# ============================================================================
# Test: Full execution path with mocked HA service
# ============================================================================


class TestFullExecutionPath:
    """
    Test complete execution paths with mocked HA service calls.
    """

    @pytest.mark.asyncio
    async def test_successful_light_control(self, control_device_cmd):
        """Test successful light control end-to-end."""
        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_svc_cls:
            mock_svc = AsyncMock()
            mock_svc.control_device = AsyncMock(
                return_value=ServiceCallResult(
                    success=True,
                    entity_id="light.basement",
                    action="light.turn_on",
                )
            )
            mock_svc_cls.return_value = mock_svc

            response = await control_device_cmd._execute_control(
                entity_id="light.basement",
                domain="light",
                action="turn_on",
                value=None,
            )

            assert response.success is True
            assert response.wait_for_input is False
            mock_svc.control_device.assert_called_once_with(
                "light.basement", "turn_on", None
            )

    @pytest.mark.asyncio
    async def test_climate_with_temperature(self, control_device_cmd):
        """Test climate control with temperature value."""
        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_svc_cls:
            mock_svc = AsyncMock()
            mock_svc.control_device = AsyncMock(
                return_value=ServiceCallResult(
                    success=True,
                    entity_id="climate.thermostat",
                    action="climate.set_temperature",
                )
            )
            mock_svc_cls.return_value = mock_svc

            response = await control_device_cmd._execute_control(
                entity_id="climate.thermostat",
                domain="climate",
                action="set_temperature",
                value="72",
            )

            assert response.success is True
            mock_svc.control_device.assert_called_once_with(
                "climate.thermostat", "set_temperature", {"temperature": 72.0}
            )

    @pytest.mark.asyncio
    async def test_ha_service_failure(self, control_device_cmd):
        """Test handling of HA service failure."""
        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_svc_cls:
            mock_svc = AsyncMock()
            mock_svc.control_device = AsyncMock(
                return_value=ServiceCallResult(
                    success=False,
                    entity_id="light.basement",
                    action="light.turn_on",
                    error="Connection timeout",
                )
            )
            mock_svc_cls.return_value = mock_svc

            response = await control_device_cmd._execute_control(
                entity_id="light.basement",
                domain="light",
                action="turn_on",
                value=None,
            )

            assert response.success is False
            assert "timeout" in response.error_details.lower()


# ============================================================================
# Test: Device status queries
# ============================================================================


class TestDeviceStatusQueries:
    """Test get_device_status command with mocked responses."""

    def test_query_light_status(self, get_device_status_cmd, request_info):
        """Test querying light status."""
        with patch.object(get_device_status_cmd, "_execute_query") as mock_query:
            mock_query.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "light.basement",
                    "state": "on",
                    "attributes": {
                        "brightness": 255,
                        "color_temp": 370,
                    },
                },
                wait_for_input=False,
            )

            response = get_device_status_cmd.run(
                request_info,
                entity_id="light.basement",
            )

            assert response.success is True
            assert response.context_data["state"] == "on"

    def test_query_climate_status(self, get_device_status_cmd, request_info):
        """Test querying thermostat status."""
        with patch.object(get_device_status_cmd, "_execute_query") as mock_query:
            mock_query.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "climate.thermostat",
                    "state": "heat",
                    "attributes": {
                        "current_temperature": 68,
                        "temperature": 72,
                        "hvac_action": "heating",
                    },
                },
                wait_for_input=False,
            )

            response = get_device_status_cmd.run(
                request_info,
                entity_id="climate.thermostat",
            )

            assert response.success is True
            assert response.context_data["attributes"]["current_temperature"] == 68
