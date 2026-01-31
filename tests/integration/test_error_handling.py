"""
Integration tests for error handling scenarios.

Tests the CommandExecutionService's handling of various error conditions.
"""

import pytest
from unittest.mock import MagicMock, patch

from tests.integration.fixtures.mock_responses import (
    create_complete_response,
    create_tool_call_response,
    create_error_response,
)


class TestCommandCenterUnreachable:
    """Test handling of communication failures with command center."""

    def test_initial_command_returns_none(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: command center returns None on initial request.

        This simulates a network error or server unavailability.
        """
        # Queue no responses - client will return None
        mock_client.queue_responses([])

        result = command_execution_service.process_voice_command(
            "What is 2+2?",
            register_tools=False
        )

        assert result["success"] is False
        assert "Command Center" in result["message"] or "communicate" in result["message"].lower()

    def test_tool_results_returns_none(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: command center returns None after tool results are sent.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "1+1"}, "id": "call_1"}
            ])
            # No second response - simulates network failure after tool execution
        ])

        result = command_execution_service.process_voice_command(
            "Calculate 1+1",
            register_tools=False
        )

        assert result["success"] is False
        assert "tool results" in result["message"].lower() or "failed" in result["message"].lower()


class TestMaxIterationsExceeded:
    """Test the safety limit for conversation iterations."""

    def test_max_iterations_safety_limit(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: conversation exceeds maximum iterations.

        This prevents infinite loops if the command center keeps returning
        tool_calls without ever completing.
        """
        # Create an infinite loop of tool calls (11 responses to exceed limit of 10)
        endless_tool_calls = [
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": f"{i}+1"}, "id": f"call_{i}"}
            ])
            for i in range(15)
        ]
        mock_client.queue_responses(endless_tool_calls)

        result = command_execution_service.process_voice_command(
            "Keep calculating forever",
            register_tools=False
        )

        assert result["success"] is False
        assert "maximum iterations" in result["message"].lower() or "exceeded" in result["message"].lower()

        # Should have stopped at max_iterations (10 loops + initial call = 11 calls max)
        # But first call is send_command, then 10 iterations of send_tool_results
        assert len(mock_client.call_history) <= 11


class TestUnknownTool:
    """Test handling of unknown tool requests."""

    def test_unknown_tool_in_tool_call(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: command center requests a tool that doesn't exist.

        The node should return an error for that tool call.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "nonexistent_tool", "arguments": {"param": "value"}, "id": "call_unknown"}
            ]),
            create_complete_response("I apologize, that tool is not available")
        ])

        result = command_execution_service.process_voice_command(
            "Use a tool that doesn't exist",
            register_tools=False
        )

        # Should complete (command center handles the error)
        assert result["success"] is True

        # Verify error was reported for the unknown tool
        tool_results = mock_client.call_history[1]["tool_results"]
        assert len(tool_results) == 1
        assert tool_results[0]["output"]["success"] is False
        assert "unknown tool" in tool_results[0]["output"]["error"].lower()

    def test_mixed_known_and_unknown_tools(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: some tools exist, some don't.

        Should execute known tools and report errors for unknown ones.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "5+5"}, "id": "call_known"},
                {"name": "make_coffee", "arguments": {}, "id": "call_unknown"}
            ]),
            create_complete_response("5+5 is 10, but I can't make coffee")
        ])

        result = command_execution_service.process_voice_command(
            "Calculate 5+5 and make coffee",
            register_tools=False
        )

        assert result["success"] is True

        tool_results = mock_client.call_history[1]["tool_results"]
        assert len(tool_results) == 2

        # Find results by tool_call_id
        known_result = next(r for r in tool_results if r["tool_call_id"] == "call_known")
        unknown_result = next(r for r in tool_results if r["tool_call_id"] == "call_unknown")

        assert known_result["output"]["success"] is True
        assert known_result["output"]["context"]["result"] == 10
        assert unknown_result["output"]["success"] is False


class TestUnknownStopReason:
    """Test handling of unexpected stop_reason values."""

    def test_unknown_stop_reason_returns_error(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: command center returns an unexpected stop_reason.
        """
        mock_client.queue_responses([
            {
                "stop_reason": "unexpected_reason",
                "assistant_message": "Something went wrong",
                "tool_calls": None,
                "validation_request": None,
                "commands": []
            }
        ])

        result = command_execution_service.process_voice_command(
            "Test unknown stop reason",
            register_tools=False
        )

        assert result["success"] is False
        assert "unknown stop_reason" in result["message"].lower() or "unexpected" in result["message"].lower()


class TestMissingStopReason:
    """Test handling of responses with missing stop_reason."""

    def test_null_stop_reason_is_error(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: response has null/missing stop_reason.

        This indicates a malformed response from the command center.
        """
        mock_client.queue_responses([
            create_error_response("Something went wrong")
        ])

        result = command_execution_service.process_voice_command(
            "Test error response",
            register_tools=False
        )

        # The response has stop_reason=None, which is not 'complete', 'tool_calls', or 'validation_required'
        # So it should be treated as an unknown stop_reason error
        assert result["success"] is False


class TestToolRegistrationFailure:
    """Test handling of tool registration failures."""

    def test_tool_registration_fails_but_continues(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: tool registration fails but command processing continues.

        Tool registration is optional - command should still be processed.
        """
        mock_client.start_conversation_result = False
        mock_client.queue_responses([
            create_complete_response("Hello! I processed your command.")
        ])

        result = command_execution_service.process_voice_command(
            "Hello",
            register_tools=True  # Enable registration (but it will fail)
        )

        # Should still succeed since command center can still process
        assert result["success"] is True


class TestConversationIdPreservation:
    """Test that conversation_id is preserved throughout the flow."""

    def test_conversation_id_consistent_across_calls(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: conversation_id is the same across all API calls.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "1+1"}, "id": "call_1"}
            ]),
            create_complete_response("The answer is 2")
        ])

        result = command_execution_service.process_voice_command(
            "What is 1+1?",
            register_tools=False
        )

        assert result["success"] is True

        # Get conversation_id from result
        conversation_id = result["conversation_id"]
        assert conversation_id is not None

        # Verify all calls used the same conversation_id
        for call in mock_client.call_history:
            assert call.get("conversation_id") == conversation_id


class TestExceptionHandling:
    """Test handling of unexpected exceptions."""

    def test_general_exception_during_processing(
        self, mock_client, mock_command_discovery, mock_config
    ):
        """
        Test: unexpected exception during command processing.
        """
        from utils.command_execution_service import CommandExecutionService

        # Create service with a client that will raise an exception
        with patch.object(
            mock_client,
            "send_command",
            side_effect=RuntimeError("Unexpected network error")
        ):
            from clients.jarvis_command_center_client import JarvisCommandCenterClient
            with patch.object(
                JarvisCommandCenterClient,
                "__new__",
                lambda cls, *args, **kwargs: mock_client
            ):
                service = CommandExecutionService()
                service.client = mock_client

                result = service.process_voice_command(
                    "Test exception",
                    register_tools=False
                )

        assert result["success"] is False
        assert "error" in result["message"].lower()
