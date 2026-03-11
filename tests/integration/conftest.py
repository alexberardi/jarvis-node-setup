"""
Shared fixtures for integration tests.

These fixtures provide common test infrastructure for all command
integration tests, ensuring consistency and reducing duplication.
"""

import sys
from unittest.mock import MagicMock


def pytest_configure(config):
    """
    Pytest hook that runs BEFORE test collection.

    We use this to mock modules that have native/external dependencies
    before pytest tries to import test files.
    """
    # Mock db module (requires pysqlcipher3 native compilation)
    if 'db' not in sys.modules:
        mock_db = MagicMock()
        mock_db.SessionLocal = MagicMock()
        sys.modules['db'] = mock_db

    # Mock secret_service (imports db)
    if 'services.secret_service' not in sys.modules:
        mock_secret_service = MagicMock()
        mock_secret_service.get_secret_value = MagicMock(return_value=None)
        sys.modules['services.secret_service'] = mock_secret_service

    # Mock jarvis_log_client (external package)
    if 'jarvis_log_client' not in sys.modules:
        mock_logger = MagicMock()
        mock_logger.JarvisLogger = MagicMock(return_value=MagicMock())
        sys.modules['jarvis_log_client'] = mock_logger

    # Mock repositories (imports sqlalchemy for db operations)
    if 'repositories' not in sys.modules:
        mock_repo = MagicMock()
        sys.modules['repositories'] = mock_repo
        sys.modules['repositories.command_data_repository'] = mock_repo


import pytest
from unittest.mock import patch

from clients.responses.jarvis_command_center.tool_calling_response import ToolCallingResponse
from core.command_response import CommandResponse
from core.request_information import RequestInformation


class MockCommandCenterClient:
    """Mock client that queues responses for integration tests.

    Works with the ``send_command_unified`` / ``send_tool_results`` path
    used by ``CommandExecutionService.process_voice_command``.
    """

    def __init__(self):
        self._response_queue: list[dict] = []
        self.call_history: list[dict] = []
        self.start_conversation_result: bool = True

    # -- helpers for tests ------------------------------------------------

    def queue_responses(self, responses: list[dict]) -> None:
        self._response_queue = list(responses)

    def _pop_response(self) -> ToolCallingResponse | None:
        if not self._response_queue:
            return None
        raw = self._response_queue.pop(0)
        return ToolCallingResponse.model_validate(raw)

    # -- mock API surface used by CommandExecutionService -------------------

    def send_command_unified(self, voice_command: str, conversation_id: str):
        resp = self._pop_response()
        self.call_history.append({
            "method": "send_command_unified",
            "voice_command": voice_command,
            "conversation_id": conversation_id,
        })
        if resp is None:
            return ("error", "Failed to communicate with Command Center")
        return ("control", resp)

    def send_command(self, voice_command: str, conversation_id: str):
        resp = self._pop_response()
        self.call_history.append({
            "method": "send_command",
            "voice_command": voice_command,
            "conversation_id": conversation_id,
        })
        return resp

    def send_tool_results(self, conversation_id: str, tool_results: list[dict]):
        resp = self._pop_response()
        self.call_history.append({
            "method": "send_tool_results",
            "conversation_id": conversation_id,
            "tool_results": tool_results,
        })
        return resp

    def send_validation_response(self, conversation_id, validation_request, user_response):
        resp = self._pop_response()
        self.call_history.append({
            "method": "send_validation_response",
            "conversation_id": conversation_id,
            "user_response": user_response,
        })
        return resp

    def start_conversation(self, *args, **kwargs):
        return self.start_conversation_result

    def get_date_context(self):
        return None


class MockCalculateCommand:
    """Minimal command mock for the ``calculate`` tool used by most error-handling tests."""

    command_name = "calculate"

    def pre_route(self, voice_command: str):
        return None

    def post_process_tool_call(self, arguments: dict, voice_command: str) -> dict:
        return arguments

    def execute(self, request_info, **kwargs):
        expression = kwargs.get("expression", "0")
        try:
            result = eval(expression)  # noqa: S307 – test only
        except Exception:
            result = None
        return CommandResponse.success_response(
            context_data={"result": result}, wait_for_input=False,
        )

    def get_command_schema(self, date_context=None):
        return {"command_name": "calculate"}

    def to_openai_tool_schema(self, date_context=None):
        return {"type": "function", "function": {"name": "calculate", "parameters": {}}}


class MockWeatherCommand:
    """Minimal command mock for the ``get_weather`` tool."""

    command_name = "get_weather"

    def pre_route(self, voice_command: str):
        return None

    def post_process_tool_call(self, arguments: dict, voice_command: str) -> dict:
        return arguments

    def execute(self, request_info, **kwargs):
        location = kwargs.get("location", "Unknown")
        return CommandResponse.success_response(
            context_data={"location": location, "temperature": 72, "condition": "sunny"},
            wait_for_input=False,
        )

    def get_command_schema(self, date_context=None):
        return {"command_name": "get_weather"}

    def to_openai_tool_schema(self, date_context=None):
        return {"type": "function", "function": {"name": "get_weather", "parameters": {}}}


class MockAlwaysFailCommand:
    """Minimal command mock that always raises an exception."""

    command_name = "always_fail"

    def pre_route(self, voice_command: str):
        return None

    def post_process_tool_call(self, arguments: dict, voice_command: str) -> dict:
        return arguments

    def execute(self, request_info, **kwargs):
        raise RuntimeError("This tool always fails")

    def get_command_schema(self, date_context=None):
        return {"command_name": "always_fail"}

    def to_openai_tool_schema(self, date_context=None):
        return {"type": "function", "function": {"name": "always_fail", "parameters": {}}}


class MockCommandDiscoveryService:
    """Minimal discovery service that returns pre-configured commands."""

    def __init__(self, commands: dict):
        self._commands = commands

    def get_all_commands(self) -> dict:
        return dict(self._commands)

    def get_command(self, name: str):
        return self._commands.get(name)

    def refresh_now(self):
        pass


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
def mock_client():
    """A mock JarvisCommandCenterClient with response queuing."""
    return MockCommandCenterClient()


@pytest.fixture
def sample_commands():
    """Dictionary of mock commands keyed by command_name."""
    commands = [MockCalculateCommand(), MockWeatherCommand(), MockAlwaysFailCommand()]
    return {cmd.command_name: cmd for cmd in commands}


@pytest.fixture
def mock_command_discovery(sample_commands):
    """A MockCommandDiscoveryService pre-loaded with sample_commands."""
    return MockCommandDiscoveryService(sample_commands)


@pytest.fixture
def mock_config():
    """Patches Config and service-discovery helpers used by CommandExecutionService.__init__."""
    with patch("utils.command_execution_service.Config") as cfg, \
         patch("utils.command_execution_service.get_command_center_url", return_value="http://test:7703"):
        cfg.get_str.return_value = "test-node"
        yield cfg


@pytest.fixture
def command_execution_service(mock_client, mock_command_discovery, mock_config):
    """A fully wired CommandExecutionService backed by mocks."""
    with patch(
        "utils.command_execution_service.get_command_discovery_service",
        return_value=mock_command_discovery,
    ):
        from utils.command_execution_service import CommandExecutionService
        service = CommandExecutionService()
        service.client = mock_client
        return service


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
