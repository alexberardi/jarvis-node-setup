"""
Shared fixtures for integration tests.

These fixtures provide common test infrastructure for all command
integration tests, ensuring consistency and reducing duplication.
"""

import pytest
from unittest.mock import MagicMock

from core.request_information import RequestInformation


@pytest.fixture
def request_info_factory():
    """Factory for creating RequestInformation with custom voice commands."""
    def _create(voice_command: str, conversation_id: str = "test-conv-123"):
        return RequestInformation(
            voice_command=voice_command,
            conversation_id=conversation_id,
        )
    return _create


@pytest.fixture
def request_info(request_info_factory):
    """Default RequestInformation for simple tests."""
    return request_info_factory("test voice command")


@pytest.fixture
def mock_success_response():
    """Factory for creating mock successful CommandResponses."""
    from core.command_response import CommandResponse
    
    def _create(context_data: dict, wait_for_input: bool = False):
        return CommandResponse.success_response(
            context_data=context_data,
            wait_for_input=wait_for_input,
        )
    return _create


@pytest.fixture
def mock_error_response():
    """Factory for creating mock error CommandResponses."""
    from core.command_response import CommandResponse
    
    def _create(error_details: str, context_data: dict = None):
        return CommandResponse.error_response(
            error_details=error_details,
            context_data=context_data or {},
        )
    return _create


@pytest.fixture
def mock_ha_context():
    """
    Simulated Home Assistant agent context.
    
    Represents the context data that would be injected from
    HomeAssistantAgent into the node_context.agents field.
    """
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
