"""
Integration tests for tool execution flow.

Tests the CommandExecutionService's handling of tool calls from the command center.

Flow being tested:
    Node → Command Center: send_command
    Command Center → Node: tool_calls response
    Node: execute tools locally
    Node → Command Center: send_tool_results
    Command Center → Node: complete response
"""

import pytest
from unittest.mock import MagicMock

from tests.integration.fixtures.mock_responses import (
    create_complete_response,
    create_tool_call_response,
)


class TestSingleToolCallSuccess:
    """Test successful single tool call execution."""

    def test_calculator_tool_call_success(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: command → tool_call → execute → results → complete

        Flow:
        1. User says "What is 2 plus 2?"
        2. Command center returns tool_call for calculate(expression="2+2")
        3. Node executes calculate locally
        4. Node sends results back
        5. Command center returns complete with "The answer is 4"
        """
        # Setup: queue responses
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "2+2"}, "id": "call_abc123"}
            ]),
            create_complete_response("The answer is 4")
        ])

        # Execute
        result = command_execution_service.process_voice_command(
            "What is 2 plus 2?",
            register_tools=False  # Skip tool registration for test
        )

        # Verify result
        assert result["success"] is True
        assert result["message"] == "The answer is 4"
        assert "conversation_id" in result

        # Verify call sequence
        assert len(mock_client.call_history) == 2

        # First call: send_command
        assert mock_client.call_history[0]["method"] == "send_command"
        assert mock_client.call_history[0]["voice_command"] == "What is 2 plus 2?"

        # Second call: send_tool_results
        assert mock_client.call_history[1]["method"] == "send_tool_results"
        tool_results = mock_client.call_history[1]["tool_results"]
        assert len(tool_results) == 1
        assert tool_results[0]["tool_call_id"] == "call_abc123"
        assert tool_results[0]["output"]["success"] is True
        assert tool_results[0]["output"]["context"]["result"] == 4

    def test_weather_tool_call_success(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test weather tool execution with location parameter.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "get_weather", "arguments": {"location": "New York"}, "id": "call_weather1"}
            ]),
            create_complete_response("It's 72 degrees and sunny in New York")
        ])

        result = command_execution_service.process_voice_command(
            "What's the weather in New York?",
            register_tools=False
        )

        assert result["success"] is True
        assert "sunny" in result["message"].lower() or "New York" in result["message"]

        # Verify tool results
        tool_results = mock_client.call_history[1]["tool_results"]
        assert tool_results[0]["output"]["context"]["location"] == "New York"
        assert tool_results[0]["output"]["context"]["temperature"] == 72


class TestMultipleToolCalls:
    """Test execution of multiple tool calls."""

    def test_sequential_tool_calls(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test multiple tool calls executed in sequence.

        Flow:
        1. Command center returns tool_call for calculate
        2. Node executes and sends results
        3. Command center returns another tool_call for get_weather
        4. Node executes and sends results
        5. Command center returns complete
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "10*5"}, "id": "call_1"}
            ]),
            create_tool_call_response([
                {"name": "get_weather", "arguments": {"location": "Chicago"}, "id": "call_2"}
            ]),
            create_complete_response("The calculation result is 50, and it's sunny in Chicago")
        ])

        result = command_execution_service.process_voice_command(
            "Calculate 10 times 5 and tell me the weather in Chicago",
            register_tools=False
        )

        assert result["success"] is True

        # Verify 3 API calls were made
        assert len(mock_client.call_history) == 3
        assert mock_client.call_history[0]["method"] == "send_command"
        assert mock_client.call_history[1]["method"] == "send_tool_results"
        assert mock_client.call_history[2]["method"] == "send_tool_results"

    def test_multiple_tools_in_single_response(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test multiple tool calls returned in a single response.

        Some LLMs can return multiple tool calls at once.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "5+5"}, "id": "call_1"},
                {"name": "get_weather", "arguments": {"location": "Boston"}, "id": "call_2"}
            ]),
            create_complete_response("5+5 is 10, and Boston weather is 72 degrees")
        ])

        result = command_execution_service.process_voice_command(
            "What is 5+5 and what's the weather in Boston?",
            register_tools=False
        )

        assert result["success"] is True

        # Verify tool results contain both results
        tool_results = mock_client.call_history[1]["tool_results"]
        assert len(tool_results) == 2

        # Find results by tool_call_id
        calc_result = next(r for r in tool_results if r["tool_call_id"] == "call_1")
        weather_result = next(r for r in tool_results if r["tool_call_id"] == "call_2")

        assert calc_result["output"]["context"]["result"] == 10
        assert weather_result["output"]["context"]["location"] == "Boston"


class TestToolExecutionError:
    """Test handling of tool execution errors."""

    def test_tool_raises_exception(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: tool execution raises an exception.

        The error should be captured and sent back to the command center.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "always_fail", "arguments": {}, "id": "call_fail"}
            ]),
            create_complete_response("I'm sorry, the operation failed")
        ])

        result = command_execution_service.process_voice_command(
            "Make it fail",
            register_tools=False
        )

        # The conversation should complete (command center handles the error)
        assert result["success"] is True
        assert "failed" in result["message"].lower() or "sorry" in result["message"].lower()

        # Verify error was sent back
        tool_results = mock_client.call_history[1]["tool_results"]
        assert tool_results[0]["output"]["success"] is False
        assert "always fails" in tool_results[0]["output"]["error"]

    def test_partial_tool_execution_failure(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: one tool succeeds, another fails.

        Both results should be sent back to the command center.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "3*3"}, "id": "call_ok"},
                {"name": "always_fail", "arguments": {}, "id": "call_fail"}
            ]),
            create_complete_response("3*3 is 9, but the other operation failed")
        ])

        result = command_execution_service.process_voice_command(
            "Calculate 3*3 and also fail",
            register_tools=False
        )

        assert result["success"] is True

        # Verify both results sent
        tool_results = mock_client.call_history[1]["tool_results"]
        assert len(tool_results) == 2

        ok_result = next(r for r in tool_results if r["tool_call_id"] == "call_ok")
        fail_result = next(r for r in tool_results if r["tool_call_id"] == "call_fail")

        assert ok_result["output"]["success"] is True
        assert ok_result["output"]["context"]["result"] == 9
        assert fail_result["output"]["success"] is False


class TestToolCallWithComplexArguments:
    """Test tool calls with various argument formats."""

    def test_tool_call_with_json_string_arguments(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: arguments provided as JSON string (as returned by some LLMs).
        """
        # Arguments as JSON string instead of dict
        mock_client.queue_responses([
            {
                "stop_reason": "tool_calls",
                "assistant_message": None,
                "tool_calls": [{
                    "id": "call_json",
                    "type": "function",
                    "function": {
                        "name": "calculate",
                        "arguments": '{"expression": "100/4"}'
                    }
                }],
                "validation_request": None,
                "commands": []
            },
            create_complete_response("100 divided by 4 is 25")
        ])

        result = command_execution_service.process_voice_command(
            "What is 100 divided by 4?",
            register_tools=False
        )

        assert result["success"] is True
        tool_results = mock_client.call_history[1]["tool_results"]
        assert tool_results[0]["output"]["context"]["result"] == 25

    def test_immediate_complete_no_tools(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: command center returns immediate complete (no tools needed).

        Some queries can be answered directly without tool calls.
        """
        mock_client.queue_responses([
            create_complete_response("Hello! How can I help you today?")
        ])

        result = command_execution_service.process_voice_command(
            "Hello",
            register_tools=False
        )

        assert result["success"] is True
        assert result["message"] == "Hello! How can I help you today?"

        # Only one call - no tool execution
        assert len(mock_client.call_history) == 1
        assert mock_client.call_history[0]["method"] == "send_command"
