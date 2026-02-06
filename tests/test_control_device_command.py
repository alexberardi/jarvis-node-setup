"""
Unit tests for ControlDeviceCommand.

Tests parameter validation, domain-based action validation,
clarification flow, and command execution.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from commands.control_device_command import ControlDeviceCommand
from core.command_response import CommandResponse
from core.request_information import RequestInformation
from services.home_assistant_service import ServiceCallResult


@pytest.fixture
def command():
    """Create a ControlDeviceCommand instance."""
    return ControlDeviceCommand()


@pytest.fixture
def request_info():
    """Create a mock RequestInformation."""
    return RequestInformation(
        voice_command="open the garage door",
        conversation_id="test-conversation-123",
    )


class TestControlDeviceCommandProperties:
    """Test command properties."""

    def test_command_name(self, command):
        """Command has correct name."""
        assert command.command_name == "control_device"

    def test_description(self, command):
        """Command has description."""
        assert "control" in command.description.lower()

    def test_keywords(self, command):
        """Command has relevant keywords."""
        keywords = command.keywords
        assert "open" in keywords
        assert "close" in keywords
        assert "lock" in keywords
        assert "unlock" in keywords

    def test_parameters(self, command):
        """Command has correct parameters."""
        params = command.parameters
        param_names = [p.name for p in params]

        assert "entity_id" in param_names
        assert "action" in param_names
        assert "value" in param_names

        # entity_id required, action optional
        entity_param = next(p for p in params if p.name == "entity_id")
        action_param = next(p for p in params if p.name == "action")
        assert entity_param.required is True
        assert action_param.required is False

    def test_required_secrets(self, command):
        """Command requires HA secrets."""
        secrets = command.required_secrets
        secret_keys = [s.key for s in secrets]

        assert "HOME_ASSISTANT_REST_URL" in secret_keys
        assert "HOME_ASSISTANT_API_KEY" in secret_keys


class TestPromptExamples:
    """Test example generation."""

    def test_generate_prompt_examples(self, command):
        """Generates valid prompt examples."""
        examples = command.generate_prompt_examples()

        assert len(examples) >= 3
        assert any(ex.is_primary for ex in examples)

        # Check structure - should have entity_id and action
        primary = next(ex for ex in examples if ex.is_primary)
        assert "entity_id" in primary.expected_parameters
        assert "action" in primary.expected_parameters

    def test_generate_adapter_examples_has_variety(self, command):
        """Generates varied adapter examples across domains."""
        examples = command.generate_adapter_examples()

        assert len(examples) >= 20

        # Should cover multiple domains and actions
        actions = {ex.expected_parameters.get("action") for ex in examples if "action" in ex.expected_parameters}

        assert "open_cover" in actions
        assert "close_cover" in actions
        assert "lock" in actions
        assert "unlock" in actions
        assert "set_temperature" in actions

    def test_adapter_examples_include_ambiguous(self, command):
        """Adapter examples include ambiguous cases without action."""
        examples = command.generate_adapter_examples()

        # Should have some examples without action (for training clarification)
        no_action_examples = [
            ex for ex in examples
            if "action" not in ex.expected_parameters
        ]
        assert len(no_action_examples) >= 1


class TestActionValidation:
    """Test domain-based action validation."""

    def test_missing_entity_id(self, command, request_info):
        """Returns error when entity_id is missing."""
        response = command.run(request_info)

        assert response.success is False
        assert "entity id" in response.error_details.lower()

    def test_invalid_entity_id_format(self, command, request_info):
        """Returns error for invalid entity_id format."""
        response = command.run(request_info, entity_id="invalid_format")

        assert response.success is False
        assert "invalid entity" in response.error_details.lower()

    def test_unknown_domain(self, command, request_info):
        """Returns error for unknown domain."""
        response = command.run(
            request_info,
            entity_id="unknown_domain.test",
            action="turn_on",
        )

        assert response.success is False
        assert "unknown device type" in response.error_details.lower()


class TestActionClarification:
    """Test clarification flow when action is missing or invalid."""

    def test_missing_action_returns_clarification(self, command, request_info):
        """Returns validation response when action is missing."""
        response = command.run(request_info, entity_id="cover.garage_door")

        # Should be a follow-up response, not error
        assert response.success is True
        assert response.wait_for_input is True
        assert response.context_data["validation_type"] == "action_required"
        assert "cover.garage_door" in response.context_data["entity_id"]
        assert "allowed_actions" in response.context_data
        assert "open_cover" in response.context_data["allowed_actions"]

    def test_invalid_action_returns_clarification(self, command, request_info):
        """Returns validation response when action is invalid for domain."""
        response = command.run(
            request_info,
            entity_id="cover.garage_door",
            action="lock",  # Invalid for cover domain
        )

        assert response.success is True
        assert response.wait_for_input is True
        assert response.context_data["validation_type"] == "action_required"
        assert "isn't valid" in response.context_data["prompt"]

    def test_clarification_includes_allowed_actions(self, command, request_info):
        """Clarification response includes domain-specific actions."""
        response = command.run(request_info, entity_id="lock.front_door")

        assert "allowed_actions" in response.context_data
        allowed = response.context_data["allowed_actions"]
        assert "lock" in allowed
        assert "unlock" in allowed
        # Should NOT include cover actions
        assert "open_cover" not in allowed

    def test_clarification_prompt_is_readable(self, command, request_info):
        """Clarification prompt uses human-friendly action names."""
        response = command.run(request_info, entity_id="cover.garage_door")

        prompt = response.context_data["prompt"]
        # Should use display names like "open" not "open_cover"
        assert "open" in prompt.lower()
        assert "close" in prompt.lower()


class TestRunCommand:
    """Test command execution."""

    def test_run_cover_open_success(self, command, request_info):
        """Successfully opens a cover."""
        with patch.object(command, "_execute_control") as mock_execute:
            mock_execute.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "cover.garage_door",
                    "domain": "cover",
                    "action": "open_cover",
                    "message": "Successfully executed open on cover.garage_door",
                },
                wait_for_input=False,
            )

            response = command.run(
                request_info,
                entity_id="cover.garage_door",
                action="open_cover",
            )

            assert response.success is True
            assert response.context_data["action"] == "open_cover"

    def test_run_lock_success(self, command, request_info):
        """Successfully locks a door."""
        with patch.object(command, "_execute_control") as mock_execute:
            mock_execute.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "lock.front_door",
                    "domain": "lock",
                    "action": "lock",
                    "message": "Successfully executed lock on lock.front_door",
                },
                wait_for_input=False,
            )

            response = command.run(
                request_info,
                entity_id="lock.front_door",
                action="lock",
            )

            assert response.success is True
            assert response.context_data["action"] == "lock"

    def test_run_climate_with_value(self, command, request_info):
        """Successfully sets temperature."""
        with patch.object(command, "_execute_control") as mock_execute:
            mock_execute.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "climate.thermostat",
                    "domain": "climate",
                    "action": "set_temperature",
                    "value": "72",
                    "message": "Successfully executed set temperature on climate.thermostat",
                },
                wait_for_input=False,
            )

            response = command.run(
                request_info,
                entity_id="climate.thermostat",
                action="set_temperature",
                value="72",
            )

            assert response.success is True
            assert response.context_data["value"] == "72"


class TestExecuteControl:
    """Test the async _execute_control method."""

    @pytest.mark.asyncio
    async def test_execute_control_success(self, command):
        """Successful control returns success response."""
        mock_result = ServiceCallResult(
            success=True,
            entity_id="cover.garage_door",
            action="cover.open_cover",
        )

        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_service_cls:
            mock_service = AsyncMock()
            mock_service.control_device = AsyncMock(return_value=mock_result)
            mock_service_cls.return_value = mock_service

            response = await command._execute_control(
                "cover.garage_door", "cover", "open_cover", None
            )

            assert response.success is True
            assert response.context_data["action"] == "open_cover"
            assert response.wait_for_input is False

    @pytest.mark.asyncio
    async def test_execute_control_with_temperature(self, command):
        """Successfully passes temperature value."""
        mock_result = ServiceCallResult(
            success=True,
            entity_id="climate.thermostat",
            action="climate.set_temperature",
        )

        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_service_cls:
            mock_service = AsyncMock()
            mock_service.control_device = AsyncMock(return_value=mock_result)
            mock_service_cls.return_value = mock_service

            response = await command._execute_control(
                "climate.thermostat", "climate", "set_temperature", "72"
            )

            assert response.success is True

            # Verify service was called with temperature data
            call_args = mock_service.control_device.call_args
            assert call_args[0][2] == {"temperature": 72.0}

    @pytest.mark.asyncio
    async def test_execute_control_invalid_temperature(self, command):
        """Returns error for invalid temperature value."""
        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_service_cls:
            mock_service = AsyncMock()
            mock_service_cls.return_value = mock_service

            response = await command._execute_control(
                "climate.thermostat", "climate", "set_temperature", "not_a_number"
            )

            assert response.success is False
            assert "invalid temperature" in response.error_details.lower()

    @pytest.mark.asyncio
    async def test_execute_control_failure(self, command):
        """Failed control returns error response."""
        mock_result = ServiceCallResult(
            success=False,
            entity_id="cover.garage_door",
            action="cover.open_cover",
            error="Connection refused",
        )

        with patch(
            "commands.control_device_command.HomeAssistantService"
        ) as mock_service_cls:
            mock_service = AsyncMock()
            mock_service.control_device = AsyncMock(return_value=mock_result)
            mock_service_cls.return_value = mock_service

            response = await command._execute_control(
                "cover.garage_door", "cover", "open_cover", None
            )

            assert response.success is False
            assert "Connection refused" in response.error_details


class TestWaitForInput:
    """Test wait_for_input behavior."""

    def test_success_does_not_wait(self, command, request_info):
        """Successful control doesn't wait for input."""
        with patch.object(command, "_execute_control") as mock_execute:
            mock_execute.return_value = CommandResponse.success_response(
                context_data={"action": "open_cover"},
                wait_for_input=False,
            )

            response = command.run(
                request_info,
                entity_id="cover.garage_door",
                action="open_cover",
            )

            assert response.wait_for_input is False

    def test_clarification_waits_for_input(self, command, request_info):
        """Clarification response waits for input."""
        response = command.run(request_info, entity_id="cover.garage_door")

        assert response.wait_for_input is True
