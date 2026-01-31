"""
Shared fixtures for integration tests.

These fixtures mock the boundaries between the node and command center,
allowing us to test the CommandExecutionService in isolation.
"""

import pytest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch
from dataclasses import dataclass

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from clients.responses.jarvis_command_center import (
    ToolCallingResponse,
    ToolCall,
    ToolCallFunction,
    ValidationRequest,
)
from core.ijarvis_command import IJarvisCommand, CommandExample
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse
from core.request_information import RequestInformation


# ---------------------------------------------------------------------------
# Sample Test Commands
# ---------------------------------------------------------------------------


class MockCalculatorCommand(IJarvisCommand):
    """Test calculator command that always succeeds."""

    @property
    def command_name(self) -> str:
        return "calculate"

    @property
    def description(self) -> str:
        return "Evaluate a mathematical expression"

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                name="expression",
                param_type="string",
                description="The math expression to evaluate",
                required=True
            )
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def keywords(self) -> List[str]:
        return ["calculate", "math", "compute"]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="What is 2 plus 2?",
                expected_parameters={"expression": "2+2"},
                is_primary=True
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return self.generate_prompt_examples()

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        expression = kwargs.get("expression", "0")
        try:
            result = eval(expression)
            return CommandResponse.success_response(
                context_data={"result": result, "expression": expression}
            )
        except Exception as e:
            return CommandResponse.error_response(str(e))


class MockWeatherCommand(IJarvisCommand):
    """Test weather command that requires a location."""

    @property
    def command_name(self) -> str:
        return "get_weather"

    @property
    def description(self) -> str:
        return "Get current weather for a location"

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                name="location",
                param_type="string",
                description="The city or location",
                required=True
            )
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def keywords(self) -> List[str]:
        return ["weather", "forecast", "temperature"]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="What's the weather in New York?",
                expected_parameters={"location": "New York"},
                is_primary=True
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return self.generate_prompt_examples()

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        location = kwargs.get("location", "Unknown")
        return CommandResponse.success_response(
            context_data={
                "location": location,
                "temperature": 72,
                "condition": "sunny"
            }
        )


class MockFailingCommand(IJarvisCommand):
    """Test command that always raises an exception."""

    @property
    def command_name(self) -> str:
        return "always_fail"

    @property
    def description(self) -> str:
        return "A command that always fails (for testing)"

    @property
    def parameters(self) -> List[JarvisParameter]:
        return []

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def keywords(self) -> List[str]:
        return ["fail", "error"]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Make it fail",
                expected_parameters={},
                is_primary=True
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return self.generate_prompt_examples()

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        raise RuntimeError("This command always fails")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_commands() -> Dict[str, IJarvisCommand]:
    """Provide a dictionary of test commands."""
    return {
        "calculate": MockCalculatorCommand(),
        "get_weather": MockWeatherCommand(),
        "always_fail": MockFailingCommand(),
    }


@pytest.fixture
def mock_command_discovery(sample_commands):
    """
    Mock the command discovery service to return our test commands.
    """
    mock_discovery = MagicMock()
    mock_discovery.get_all_commands.return_value = sample_commands
    mock_discovery.get_command.side_effect = lambda name: sample_commands.get(name)
    mock_discovery.refresh_now.return_value = None

    with patch(
        "utils.command_execution_service.get_command_discovery_service",
        return_value=mock_discovery
    ):
        yield mock_discovery


@pytest.fixture
def mock_config():
    """
    Mock the Config service to return test values.
    """
    config_values = {
        "jarvis_command_center_api_url": "http://test-server:8002",
        "node_id": "test-node-1",
        "room": "test-room",
    }

    with patch(
        "utils.command_execution_service.Config.get_str",
        side_effect=lambda key, default=None: config_values.get(key, default)
    ):
        yield config_values


class MockCommandCenterClient:
    """
    Mock client that can be configured with response sequences.

    Usage:
        client = MockCommandCenterClient()
        client.queue_responses([
            create_tool_call_response([{"name": "calculate", "arguments": {"expression": "2+2"}}]),
            create_complete_response("The answer is 4")
        ])
    """

    def __init__(self):
        self.responses: List[Dict[str, Any]] = []
        self.call_history: List[Dict[str, Any]] = []
        self._response_index = 0
        self.start_conversation_result = True
        self.date_context = None

    def queue_responses(self, responses: List[Dict[str, Any]]) -> None:
        """Queue a sequence of responses to be returned in order."""
        self.responses = responses
        self._response_index = 0

    def _get_next_response(self) -> Optional[ToolCallingResponse]:
        """Get the next queued response."""
        if self._response_index >= len(self.responses):
            return None
        response = self.responses[self._response_index]
        self._response_index += 1
        return ToolCallingResponse.model_validate(response)

    def get_date_context(self):
        """Mock get_date_context."""
        return self.date_context

    def start_conversation(
        self,
        conversation_id: str,
        commands: dict,
        date_context=None
    ) -> bool:
        """Mock start_conversation."""
        self.call_history.append({
            "method": "start_conversation",
            "conversation_id": conversation_id,
            "commands": commands,
            "date_context": date_context
        })
        return self.start_conversation_result

    def send_command(
        self,
        voice_command: str,
        conversation_id: str
    ) -> Optional[ToolCallingResponse]:
        """Mock send_command."""
        self.call_history.append({
            "method": "send_command",
            "voice_command": voice_command,
            "conversation_id": conversation_id
        })
        return self._get_next_response()

    def send_tool_results(
        self,
        conversation_id: str,
        tool_results: List[Dict[str, Any]]
    ) -> Optional[ToolCallingResponse]:
        """Mock send_tool_results."""
        self.call_history.append({
            "method": "send_tool_results",
            "conversation_id": conversation_id,
            "tool_results": tool_results
        })
        return self._get_next_response()

    def send_validation_response(
        self,
        conversation_id: str,
        validation_request: ValidationRequest,
        user_response: str
    ) -> Optional[ToolCallingResponse]:
        """Mock send_validation_response."""
        self.call_history.append({
            "method": "send_validation_response",
            "conversation_id": conversation_id,
            "validation_request": validation_request,
            "user_response": user_response
        })
        return self._get_next_response()


@pytest.fixture
def mock_client() -> MockCommandCenterClient:
    """Provide a mock command center client."""
    return MockCommandCenterClient()


@pytest.fixture
def command_execution_service(mock_client, mock_command_discovery, mock_config):
    """
    Create a CommandExecutionService with mocked dependencies.
    """
    from utils.command_execution_service import CommandExecutionService

    # Patch the client class to return our mock
    with patch.object(
        JarvisCommandCenterClient,
        "__new__",
        lambda cls, *args, **kwargs: mock_client
    ):
        service = CommandExecutionService()
        # Replace the client with our mock
        service.client = mock_client
        yield service
