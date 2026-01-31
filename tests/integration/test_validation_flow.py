"""
Integration tests for validation/clarification flow.

Tests the CommandExecutionService's handling of validation requests
when the command center needs user clarification.

Flow being tested:
    Node → Command Center: send_command
    Command Center → Node: validation_required response
    Node: prompt user for clarification
    Node → Command Center: send_validation_response
    Command Center → Node: tool_calls or complete response
"""

import pytest
from unittest.mock import MagicMock

from clients.responses.jarvis_command_center import ValidationRequest
from tests.integration.fixtures.mock_responses import (
    create_complete_response,
    create_tool_call_response,
    create_validation_response,
)


class TestValidationWithOptions:
    """Test validation requests with predefined options."""

    def test_validation_with_option_selection(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: validation request → user selects from options → complete

        Flow:
        1. User asks "What's the weather?"
        2. Command center asks for location with options
        3. User selects "New York"
        4. Command center returns complete with weather
        """
        mock_client.queue_responses([
            create_validation_response(
                question="Which city do you want the weather for?",
                parameter_name="location",
                options=["New York", "Los Angeles", "Chicago"],
                tool_call_id="validation_loc1"
            ),
            create_complete_response("It's 72 degrees and sunny in New York")
        ])

        # Provide a validation handler that selects the first option
        def validation_handler(validation: ValidationRequest) -> str:
            assert validation.question == "Which city do you want the weather for?"
            assert "New York" in validation.options
            return "New York"

        result = command_execution_service.process_voice_command(
            "What's the weather?",
            validation_handler=validation_handler,
            register_tools=False
        )

        assert result["success"] is True
        assert "New York" in result["message"]

        # Verify call sequence
        assert len(mock_client.call_history) == 2
        assert mock_client.call_history[0]["method"] == "send_command"
        assert mock_client.call_history[1]["method"] == "send_validation_response"
        assert mock_client.call_history[1]["user_response"] == "New York"

    def test_validation_without_options(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: validation request without predefined options (free-form input).
        """
        mock_client.queue_responses([
            create_validation_response(
                question="What expression would you like me to calculate?",
                parameter_name="expression",
                options=None,
                tool_call_id="validation_expr1"
            ),
            create_complete_response("The answer is 42")
        ])

        def validation_handler(validation: ValidationRequest) -> str:
            assert validation.options is None
            return "6 * 7"

        result = command_execution_service.process_voice_command(
            "Calculate something",
            validation_handler=validation_handler,
            register_tools=False
        )

        assert result["success"] is True


class TestValidationThenToolCall:
    """Test validation followed by tool execution."""

    def test_validation_then_tool_execution(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: validation → tool_call → results → complete

        Flow:
        1. User asks vague question
        2. Command center requests clarification
        3. User provides clarification
        4. Command center returns tool_call
        5. Node executes tool
        6. Command center returns complete
        """
        mock_client.queue_responses([
            create_validation_response(
                question="What would you like to calculate?",
                parameter_name="expression",
                options=None,
                tool_call_id="validation_calc1"
            ),
            create_tool_call_response([
                {"name": "calculate", "arguments": {"expression": "15+27"}, "id": "call_calc1"}
            ]),
            create_complete_response("15 plus 27 equals 42")
        ])

        def validation_handler(validation: ValidationRequest) -> str:
            return "15+27"

        result = command_execution_service.process_voice_command(
            "Do some math",
            validation_handler=validation_handler,
            register_tools=False
        )

        assert result["success"] is True
        assert "42" in result["message"]

        # Verify call sequence: send_command → validation_response → tool_results
        assert len(mock_client.call_history) == 3
        assert mock_client.call_history[0]["method"] == "send_command"
        assert mock_client.call_history[1]["method"] == "send_validation_response"
        assert mock_client.call_history[2]["method"] == "send_tool_results"


class TestValidationChain:
    """Test multiple validation requests in sequence."""

    def test_multiple_validations_in_sequence(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: validation → validation → complete

        Some commands may need multiple clarifications.
        """
        mock_client.queue_responses([
            create_validation_response(
                question="Which calculation would you like?",
                parameter_name="operation",
                options=["Addition", "Subtraction", "Multiplication"],
                tool_call_id="validation_op1"
            ),
            create_validation_response(
                question="What numbers would you like to use?",
                parameter_name="numbers",
                options=None,
                tool_call_id="validation_nums1"
            ),
            create_complete_response("10 plus 20 equals 30")
        ])

        validation_responses = iter(["Addition", "10 and 20"])

        def validation_handler(validation: ValidationRequest) -> str:
            return next(validation_responses)

        result = command_execution_service.process_voice_command(
            "Help me with math",
            validation_handler=validation_handler,
            register_tools=False
        )

        assert result["success"] is True

        # Verify two validation responses
        assert len(mock_client.call_history) == 3
        assert mock_client.call_history[1]["method"] == "send_validation_response"
        assert mock_client.call_history[2]["method"] == "send_validation_response"

    def test_validation_after_tool_execution(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: tool_call → results → validation → complete

        Command center might need clarification after seeing tool results.
        """
        mock_client.queue_responses([
            create_tool_call_response([
                {"name": "get_weather", "arguments": {"location": "Paris"}, "id": "call_w1"}
            ]),
            create_validation_response(
                question="Would you like the forecast in Celsius or Fahrenheit?",
                parameter_name="unit",
                options=["Celsius", "Fahrenheit"],
                tool_call_id="validation_unit1"
            ),
            create_complete_response("It's 22 degrees Celsius in Paris")
        ])

        def validation_handler(validation: ValidationRequest) -> str:
            return "Celsius"

        result = command_execution_service.process_voice_command(
            "What's the weather in Paris?",
            validation_handler=validation_handler,
            register_tools=False
        )

        assert result["success"] is True
        assert "Celsius" in result["message"]

        # tool_results → validation_response
        assert mock_client.call_history[1]["method"] == "send_tool_results"
        assert mock_client.call_history[2]["method"] == "send_validation_response"


class TestDefaultValidationHandler:
    """Test behavior with default validation handler."""

    def test_default_handler_provides_fallback(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: using default validation handler when none provided.

        The default handler should provide a reasonable fallback response.
        """
        mock_client.queue_responses([
            create_validation_response(
                question="Which city?",
                parameter_name="location",
                options=["NYC", "LA"],
                tool_call_id="validation_default1"
            ),
            create_complete_response("I understand. Please try again with more details.")
        ])

        # Don't provide a validation handler - use default
        result = command_execution_service.process_voice_command(
            "Weather please",
            validation_handler=None,  # Use default
            register_tools=False
        )

        # Should still complete (default handler provides some response)
        assert result["success"] is True

        # Verify validation response was sent
        assert mock_client.call_history[1]["method"] == "send_validation_response"


class TestValidationRequestDetails:
    """Test correct handling of validation request details."""

    def test_validation_preserves_tool_call_id(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: tool_call_id is preserved in validation response.
        """
        mock_client.queue_responses([
            create_validation_response(
                question="Which location?",
                parameter_name="location",
                options=["A", "B"],
                tool_call_id="specific_validation_id_123"
            ),
            create_complete_response("Done!")
        ])

        def validation_handler(validation: ValidationRequest) -> str:
            # Verify the tool_call_id is accessible
            assert validation.tool_call_id == "specific_validation_id_123"
            return "A"

        result = command_execution_service.process_voice_command(
            "Test",
            validation_handler=validation_handler,
            register_tools=False
        )

        assert result["success"] is True

    def test_validation_passes_parameter_name(
        self, command_execution_service, mock_client, sample_commands
    ):
        """
        Test: parameter_name is correctly passed in validation request.
        """
        mock_client.queue_responses([
            create_validation_response(
                question="Enter the expression:",
                parameter_name="expression",
                options=None,
                tool_call_id="val_param1"
            ),
            create_complete_response("Result: 100")
        ])

        captured_param_name = None

        def validation_handler(validation: ValidationRequest) -> str:
            nonlocal captured_param_name
            captured_param_name = validation.parameter_name
            return "50*2"

        result = command_execution_service.process_voice_command(
            "Calculate",
            validation_handler=validation_handler,
            register_tools=False
        )

        assert captured_param_name == "expression"
