"""
Unit tests for GetDeviceStatusCommand.

Tests parameter validation, state queries, and attribute filtering.
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from commands.get_device_status_command import GetDeviceStatusCommand
from core.command_response import CommandResponse
from core.request_information import RequestInformation
from services.home_assistant_service import EntityStateResult


@pytest.fixture
def command():
    """Create a GetDeviceStatusCommand instance."""
    return GetDeviceStatusCommand()


@pytest.fixture
def request_info():
    """Create a mock RequestInformation."""
    return RequestInformation(
        voice_command="is the garage door open",
        conversation_id="test-conversation-123",
    )


class TestGetDeviceStatusCommandProperties:
    """Test command properties."""

    def test_command_name(self, command):
        """Command has correct name."""
        assert command.command_name == "get_device_status"

    def test_description(self, command):
        """Command has description."""
        assert "status" in command.description.lower()

    def test_keywords(self, command):
        """Command has relevant keywords."""
        keywords = command.keywords
        assert "status" in keywords
        assert "is" in keywords
        assert "check" in keywords

    def test_parameters(self, command):
        """Command has entity_id parameter."""
        params = command.parameters
        param_names = [p.name for p in params]

        assert "entity_id" in param_names
        entity_param = next(p for p in params if p.name == "entity_id")
        assert entity_param.required is True

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

        # Check structure
        primary = next(ex for ex in examples if ex.is_primary)
        assert "entity_id" in primary.expected_parameters

    def test_generate_adapter_examples_has_variety(self, command):
        """Generates varied adapter examples across domains."""
        examples = command.generate_adapter_examples()

        assert len(examples) >= 15

        # Should cover multiple domains
        entity_ids = [ex.expected_parameters.get("entity_id", "") for ex in examples]
        domains = {eid.split(".")[0] for eid in entity_ids if "." in eid}

        assert "cover" in domains
        assert "lock" in domains
        assert "climate" in domains


class TestRunCommand:
    """Test command execution."""

    def test_run_missing_entity_id(self, command, request_info):
        """Returns error when entity_id is missing."""
        response = command.run(request_info)

        assert response.success is False
        assert "entity id" in response.error_details.lower()

    def test_run_success(self, command, request_info):
        """Successful query returns state data."""
        mock_result = EntityStateResult(
            success=True,
            entity_id="cover.garage_door",
            state="closed",
            attributes={"friendly_name": "Garage Door", "current_position": 0},
            friendly_name="Garage Door",
        )

        with patch.object(command, "_execute_query") as mock_execute:
            mock_execute.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "cover.garage_door",
                    "state": "closed",
                    "friendly_name": "Garage Door",
                    "domain": "cover",
                    "attributes": {"current_position": 0},
                },
                wait_for_input=True,
            )

            response = command.run(request_info, entity_id="cover.garage_door")

            assert response.success is True
            assert response.context_data["state"] == "closed"
            assert response.context_data["domain"] == "cover"

    def test_run_entity_not_found(self, command, request_info):
        """Returns error when entity not found."""
        with patch.object(command, "_execute_query") as mock_execute:
            mock_execute.return_value = CommandResponse.error_response(
                error_details="Could not get status: Entity 'cover.nonexistent' not found",
                context_data={"error": "Entity 'cover.nonexistent' not found"},
            )

            response = command.run(request_info, entity_id="cover.nonexistent")

            assert response.success is False
            assert "not found" in response.error_details.lower()


class TestExecuteQuery:
    """Test the async _execute_query method."""

    @pytest.mark.asyncio
    async def test_execute_query_success(self, command):
        """Successful query returns state data."""
        mock_result = EntityStateResult(
            success=True,
            entity_id="climate.thermostat",
            state="heat",
            attributes={
                "friendly_name": "Thermostat",
                "current_temperature": 68,
                "temperature": 72,
                "hvac_modes": ["heat", "cool", "off"],
                "supported_features": 17,  # Should be filtered out
            },
            friendly_name="Thermostat",
        )

        with patch(
            "commands.get_device_status_command.HomeAssistantService"
        ) as mock_service_cls:
            mock_service = AsyncMock()
            mock_service.get_state = AsyncMock(return_value=mock_result)
            mock_service_cls.return_value = mock_service

            response = await command._execute_query("climate.thermostat")

            assert response.success is True
            assert response.context_data["state"] == "heat"
            assert response.context_data["domain"] == "climate"
            # Should have relevant attributes
            assert "current_temperature" in response.context_data["attributes"]
            # Should NOT have filtered attributes
            assert "supported_features" not in response.context_data.get("attributes", {})

    @pytest.mark.asyncio
    async def test_execute_query_failure(self, command):
        """Failed query returns error response."""
        mock_result = EntityStateResult(
            success=False,
            entity_id="cover.garage_door",
            error="Connection refused",
        )

        with patch(
            "commands.get_device_status_command.HomeAssistantService"
        ) as mock_service_cls:
            mock_service = AsyncMock()
            mock_service.get_state = AsyncMock(return_value=mock_result)
            mock_service_cls.return_value = mock_service

            response = await command._execute_query("cover.garage_door")

            assert response.success is False
            assert "Connection refused" in response.error_details


class TestFilterRelevantAttributes:
    """Test attribute filtering."""

    def test_filters_internal_attributes(self, command):
        """Filters out internal/technical attributes."""
        attributes = {
            "friendly_name": "Test Device",
            "supported_features": 17,
            "icon": "mdi:lightbulb",
            "current_temperature": 68,
        }

        filtered = command._filter_relevant_attributes("climate", attributes)

        assert "friendly_name" in filtered
        assert "current_temperature" in filtered
        assert "supported_features" not in filtered
        assert "icon" not in filtered

    def test_climate_attributes(self, command):
        """Includes climate-relevant attributes."""
        attributes = {
            "current_temperature": 68,
            "temperature": 72,
            "hvac_modes": ["heat", "cool", "off"],
            "supported_features": 17,
        }

        filtered = command._filter_relevant_attributes("climate", attributes)

        assert "current_temperature" in filtered
        assert "temperature" in filtered
        assert "hvac_modes" in filtered

    def test_cover_attributes(self, command):
        """Includes cover-relevant attributes."""
        attributes = {
            "current_position": 50,
            "device_class": "garage",
            "supported_features": 15,
        }

        filtered = command._filter_relevant_attributes("cover", attributes)

        assert "current_position" in filtered
        assert "device_class" not in filtered


class TestWaitForInput:
    """Test wait_for_input behavior."""

    def test_success_waits_for_input(self, command, request_info):
        """Status queries allow follow-up questions."""
        with patch.object(command, "_execute_query") as mock_execute:
            mock_execute.return_value = CommandResponse.success_response(
                context_data={
                    "entity_id": "cover.garage_door",
                    "state": "closed",
                },
                wait_for_input=True,
            )

            response = command.run(request_info, entity_id="cover.garage_door")

            assert response.wait_for_input is True
