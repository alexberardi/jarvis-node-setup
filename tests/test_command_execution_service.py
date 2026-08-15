"""
Unit tests for CommandExecutionService.

Tests ToolExecutionResult aggregation, _execute_tools signal propagation,
process_voice_command return shape, and continue_conversation behavior.
"""

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from clients.responses.jarvis_command_center import (
    ToolCall,
    ToolCallFunction,
    ToolCallingResponse,
    ValidationRequest,
)
from core.command_response import CommandResponse
from core.request_information import RequestInformation
from utils.command_execution_service import CommandExecutionService, ToolExecutionResult, _build_secrets


# ---------- ToolExecutionResult tests ----------


class TestToolExecutionResult:
    """Test the ToolExecutionResult dataclass."""

    def test_defaults(self):
        result = ToolExecutionResult()
        assert result.api_results == []
        assert result.wait_for_input is False
        assert result.clear_history is False

    def test_custom_values(self):
        result = ToolExecutionResult(
            api_results=[{"tool_call_id": "1", "output": {}}],
            wait_for_input=True,
            clear_history=True,
        )
        assert len(result.api_results) == 1
        assert result.wait_for_input is True
        assert result.clear_history is True


# ---------- Helpers ----------


def _make_tool_call(name: str, args: str = "{}", call_id: str = "tc-1") -> ToolCall:
    """Create a ToolCall for testing."""
    return ToolCall(
        id=call_id,
        type="function",
        function=ToolCallFunction(name=name, arguments=args),
    )


def _make_final_response(message: str = "Done.") -> ToolCallingResponse:
    """Create a final ToolCallingResponse."""
    return ToolCallingResponse(
        stop_reason="complete",
        assistant_message=message,
    )


def _make_tool_calls_response(tool_calls: list[ToolCall]) -> ToolCallingResponse:
    """Create a ToolCallingResponse that requires tool execution."""
    return ToolCallingResponse(
        stop_reason="tool_calls",
        tool_calls=tool_calls,
    )


@pytest.fixture
def mock_deps():
    """Patch all CommandExecutionService dependencies."""
    with (
        patch("utils.command_execution_service.get_command_center_url", return_value="http://localhost:7703"),
        patch("utils.command_execution_service.Config") as mock_config,
        patch("utils.command_execution_service.get_command_discovery_service") as mock_discovery_fn,
        patch("utils.command_execution_service.JarvisCommandCenterClient") as mock_client_cls,
    ):
        mock_config.get_str.return_value = "test-node"

        mock_discovery = MagicMock()
        mock_discovery.get_all_commands.return_value = {}
        mock_discovery_fn.return_value = mock_discovery

        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        yield {
            "config": mock_config,
            "discovery": mock_discovery,
            "client": mock_client,
        }


# ---------- _execute_tools tests ----------


class TestExecuteTools:
    """Test _execute_tools signal aggregation."""

    def test_no_tools_returns_empty_result(self, mock_deps):
        service = CommandExecutionService()
        result = service._execute_tools([], "conv-1")

        assert isinstance(result, ToolExecutionResult)
        assert result.api_results == []
        assert result.wait_for_input is False
        assert result.clear_history is False

    def test_single_tool_propagates_wait_for_input(self, mock_deps):
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={"msg": "hi"},
            success=True,
            wait_for_input=True,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("chat", '{"message": "hello"}')
        result = service._execute_tools([tool_call], "conv-1")

        assert result.wait_for_input is True
        assert result.clear_history is False
        assert len(result.api_results) == 1

    def test_single_tool_propagates_clear_history(self, mock_deps):
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={},
            success=True,
            wait_for_input=True,
            clear_history=True,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("some_cmd")
        result = service._execute_tools([tool_call], "conv-1")

        assert result.wait_for_input is True
        assert result.clear_history is True

    def test_or_aggregation_across_multiple_tools(self, mock_deps):
        """If ANY tool returns wait_for_input, the result should be True."""
        service = CommandExecutionService()

        # First tool: wait_for_input=False
        cmd1 = MagicMock()
        cmd1.execute.return_value = CommandResponse(
            context_data={"weather": "sunny"},
            success=True,
            wait_for_input=False,
        )

        # Second tool: wait_for_input=True
        cmd2 = MagicMock()
        cmd2.execute.return_value = CommandResponse(
            context_data={"msg": "hi"},
            success=True,
            wait_for_input=True,
        )

        mock_deps["discovery"].get_command.side_effect = [cmd1, cmd2]

        tool_calls = [
            _make_tool_call("get_weather", call_id="tc-1"),
            _make_tool_call("chat", '{"message": "hi"}', call_id="tc-2"),
        ]
        result = service._execute_tools(tool_calls, "conv-1")

        assert result.wait_for_input is True
        assert len(result.api_results) == 2

    def test_unknown_tool_produces_error_result(self, mock_deps):
        service = CommandExecutionService()
        mock_deps["discovery"].get_command.return_value = None

        tool_call = _make_tool_call("nonexistent_tool")
        result = service._execute_tools([tool_call], "conv-1")

        assert len(result.api_results) == 1
        assert result.api_results[0]["output"]["success"] is False
        assert result.wait_for_input is False

    def test_tool_exception_produces_error_result(self, mock_deps):
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.side_effect = RuntimeError("boom")
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("broken_cmd")
        result = service._execute_tools([tool_call], "conv-1")

        assert len(result.api_results) == 1
        assert result.api_results[0]["output"]["success"] is False
        assert "boom" in result.api_results[0]["output"]["error"]


# ---------- process_voice_command tests ----------


class TestProcessVoiceCommand:
    """Test process_voice_command return shape and signals."""

    def test_returns_wait_for_input_and_clear_history(self, mock_deps):
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={"msg": "hi"},
            success=True,
            wait_for_input=True,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        # Server returns tool_calls, then a response after tool results
        tool_call = _make_tool_call("chat", '{"message": "hi"}')
        mock_deps["client"].send_command.return_value = _make_tool_calls_response([tool_call])
        mock_deps["client"].send_tool_results.return_value = _make_final_response("Hey there!")
        mock_deps["client"].start_conversation.return_value = True

        result = service.process_voice_command("hi")

        assert result["success"] is True
        assert result["message"] == "Hey there!"
        assert result["wait_for_input"] is True
        assert result["clear_history"] is False
        assert "conversation_id" in result
        # Tool results ARE sent back — loop breaks after one round-trip
        mock_deps["client"].send_tool_results.assert_called_once()

    def test_returns_false_signals_when_no_tools_executed(self, mock_deps):
        """Direct answer from LLM (no tool calls) should have wait_for_input=False."""
        service = CommandExecutionService()

        mock_deps["client"].send_command.return_value = _make_final_response("Sure thing!")
        mock_deps["client"].start_conversation.return_value = True

        result = service.process_voice_command("what's 2+2")

        assert result["success"] is True
        assert result["wait_for_input"] is False
        assert result["clear_history"] is False

    def test_error_result_has_no_signal_fields(self, mock_deps):
        """Error results don't include wait_for_input/clear_history."""
        service = CommandExecutionService()

        mock_deps["client"].send_command.return_value = None
        mock_deps["client"].start_conversation.return_value = True

        result = service.process_voice_command("hello")

        assert result["success"] is False
        assert "conversation_id" in result

    def test_threads_voice_command_to_request_info(self, mock_deps):
        """process_voice_command threads the real voice command to RequestInformation."""
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={"result": "data"},
            success=True,
            wait_for_input=False,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("get_device_status", '{"entity_id": "light.office"}')
        mock_deps["client"].send_command.return_value = _make_tool_calls_response([tool_call])
        mock_deps["client"].send_tool_results.return_value = _make_final_response("The light is on.")
        mock_deps["client"].start_conversation.return_value = True

        service.process_voice_command("Is the office light on?")

        # Verify RequestInformation.voice_command is the real voice command
        call_args = mock_command.execute.call_args
        request_info = call_args[0][0]
        assert isinstance(request_info, RequestInformation)
        assert request_info.voice_command == "Is the office light on?"

    def test_backward_compatible_keys(self, mock_deps):
        """Result always contains success, message, conversation_id."""
        service = CommandExecutionService()

        mock_deps["client"].send_command.return_value = _make_final_response("Done.")
        mock_deps["client"].start_conversation.return_value = True

        result = service.process_voice_command("test")

        assert "success" in result
        assert "message" in result
        assert "conversation_id" in result


# ---------- continue_conversation tests ----------


class TestContinueConversation:
    """Test continue_conversation method."""

    def test_sends_message_with_existing_conversation_id(self, mock_deps):
        service = CommandExecutionService()

        mock_deps["client"].send_command.return_value = _make_final_response("Got it!")

        result = service.continue_conversation("conv-existing", "tell me more")

        mock_deps["client"].send_command.assert_called_once_with(
            "tell me more", "conv-existing",
            turn_source=None, follow_up_iteration=None,
        )
        assert result["success"] is True
        assert result["conversation_id"] == "conv-existing"

    def test_does_not_register_tools(self, mock_deps):
        """continue_conversation should NOT re-register tools."""
        service = CommandExecutionService()

        mock_deps["client"].send_command.return_value = _make_final_response("Sure!")

        service.continue_conversation("conv-123", "follow up")

        # start_conversation should not be called during continue
        # (it was called once during __init__ via refresh_now, but not for this method)
        mock_deps["client"].start_conversation.assert_not_called()

    def test_handles_tool_calls_in_continuation(self, mock_deps):
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={"result": "data"},
            success=True,
            wait_for_input=False,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("some_cmd", '{"param": "value"}')
        mock_deps["client"].send_command.return_value = _make_tool_calls_response([tool_call])
        mock_deps["client"].send_tool_results.return_value = _make_final_response("Processed!")

        result = service.continue_conversation("conv-456", "do something")

        assert result["success"] is True
        assert result["message"] == "Processed!"

    def test_error_on_failed_communication(self, mock_deps):
        service = CommandExecutionService()

        mock_deps["client"].send_command.return_value = None

        result = service.continue_conversation("conv-789", "hello again")

        assert result["success"] is False

    def test_threads_follow_up_message_to_request_info(self, mock_deps):
        """continue_conversation threads the follow-up message to RequestInformation."""
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={"result": "data"},
            success=True,
            wait_for_input=False,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("some_cmd", '{"param": "value"}')
        mock_deps["client"].send_command.return_value = _make_tool_calls_response([tool_call])
        mock_deps["client"].send_tool_results.return_value = _make_final_response("Done!")

        service.continue_conversation("conv-456", "close the garage door")

        # Verify RequestInformation.voice_command is the follow-up message
        call_args = mock_command.execute.call_args
        request_info = call_args[0][0]
        assert isinstance(request_info, RequestInformation)
        assert request_info.voice_command == "close the garage door"

    def test_propagates_wait_for_input_signal(self, mock_deps):
        """Signals from tools should propagate through continue_conversation."""
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={"msg": "sure"},
            success=True,
            wait_for_input=True,
            clear_history=False,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("chat", '{"message": "more"}')
        mock_deps["client"].send_command.return_value = _make_tool_calls_response([tool_call])
        mock_deps["client"].send_tool_results.return_value = _make_final_response("Let's keep going!")

        result = service.continue_conversation("conv-multi", "tell me more")

        assert result["wait_for_input"] is True
        assert result["clear_history"] is False


# ---------- not_for_me silent-abort tests ----------


class TestNotForMeSilentAbort:
    """When the server signals not_for_me, the node returns a silent-abort
    dict so the caller skips TTS and the follow-up loop exits immediately."""

    def test_returns_not_for_me_and_audio_played(self, mock_deps):
        service = CommandExecutionService()

        not_for_me = ToolCallingResponse(
            stop_reason="not_for_me",
            assistant_message=None,
        )
        mock_deps["client"].send_command.return_value = not_for_me

        result = service.continue_conversation("conv-x", "and then she said")

        assert result["success"] is True
        assert result["message"] == ""
        assert result["not_for_me"] is True
        assert result["audio_played"] is True
        # clear_history stays False — only ``not_for_me`` should short-circuit
        # the follow-up loop; one-shot commands that legitimately set
        # clear_history must still allow follow-ups.
        assert result["clear_history"] is False
        # No tool round-trip should be needed.
        mock_deps["client"].send_tool_results.assert_not_called()

    def test_skips_tool_loop_entirely(self, mock_deps):
        """A not_for_me response must short-circuit before any tool execution
        or validation handling — even if tool_calls happened to be present."""
        service = CommandExecutionService()

        # Misshapen response: stop_reason=not_for_me but also has tool_calls.
        # The not_for_me signal wins; tools must not execute.
        tc = _make_tool_call("should_never_run")
        weird = ToolCallingResponse(
            stop_reason="not_for_me",
            tool_calls=[tc],
        )
        mock_deps["client"].send_command.return_value = weird

        validation_handler = MagicMock()
        result = service.continue_conversation(
            "conv-y", "ambient", validation_handler=validation_handler,
        )

        assert result["not_for_me"] is True
        validation_handler.assert_not_called()
        mock_deps["discovery"].get_command.assert_not_called()

    def test_not_for_me_on_tool_results_round_is_terminal(self, mock_deps):
        """The sentinel arriving on the CONTINUE round (send_tool_results)
        must be terminal — before this fix it fell through to the
        wait_for_input break, the terminal dict carried no not_for_me key,
        and the follow-up loop kept listening AND spoke the tool message it
        should have suppressed (prod 2026-08-15 23:07 UTC)."""
        service = CommandExecutionService()

        mock_command = MagicMock()
        mock_command.execute.return_value = CommandResponse(
            context_data={"msg": "chatty"},
            success=True,
            wait_for_input=True,
            clear_history=False,
        )
        mock_deps["discovery"].get_command.return_value = mock_command

        tool_call = _make_tool_call("chat", '{"message": "hm"}')
        mock_deps["client"].send_command.return_value = _make_tool_calls_response([tool_call])
        mock_deps["client"].send_tool_results.return_value = ToolCallingResponse(
            stop_reason="not_for_me",
            assistant_message=None,
        )

        result = service.continue_conversation("conv-cont", "ambient chatter")

        assert result["not_for_me"] is True
        assert result["message"] == ""
        assert result["audio_played"] is True
        # wait_for_input from the tool must NOT survive the sentinel — the
        # follow-up loop exits on not_for_me, it doesn't wait for input.
        assert result["wait_for_input"] is False

    def test_not_for_me_on_validation_round_is_terminal(self, mock_deps):
        """Same terminal rule for the validation continue round."""
        service = CommandExecutionService()

        validation_response = ToolCallingResponse(
            stop_reason="validation_required",
            validation_request=ValidationRequest(
                question="Which room?",
                parameter_name="room",
                tool_call_id="tc-1",
            ),
        )
        mock_deps["client"].send_command.return_value = validation_response
        mock_deps["client"].send_validation_response.return_value = ToolCallingResponse(
            stop_reason="not_for_me",
            assistant_message=None,
        )

        validation_handler = MagicMock(return_value="the kitchen")
        result = service.continue_conversation(
            "conv-val", "turn it off", validation_handler=validation_handler,
        )

        assert result["not_for_me"] is True
        assert result["message"] == ""
        assert result["wait_for_input"] is False


# ---------- _build_secrets tests ----------


class TestBuildSecrets:
    """Regression coverage for the ContextVar wiring in _build_secrets.

    Bug history: this helper used to call get_secret_value(key, scope) with no
    user_id, silently dropping every scope='user' value because the repo
    requires user_id to match the stored row. Only callers that bypass this
    helper (via JarvisStorage, which reads the ContextVar internally) worked.
    """

    def test_user_scope_passes_contextvar_user_id(self):
        from jarvis_command_sdk import JarvisSecret
        from jarvis_command_sdk.context import set_current_user_id

        command = MagicMock()
        command.required_secrets = [
            JarvisSecret("USER_TOKEN", "User token", "user", "string"),
            JarvisSecret("SHARED_KEY", "Shared key", "integration", "string"),
        ]

        set_current_user_id(42)
        try:
            with patch("services.secret_service.get_secret_value") as mock_get:
                mock_get.side_effect = lambda key, scope, user_id=None: f"{key}:{user_id}"
                result = _build_secrets(command)

                mock_get.assert_any_call("USER_TOKEN", "user", user_id=42)
                mock_get.assert_any_call("SHARED_KEY", "integration", user_id=None)
                assert result == {"USER_TOKEN": "USER_TOKEN:42", "SHARED_KEY": "SHARED_KEY:None"}
        finally:
            set_current_user_id(None)

    def test_user_scope_with_unset_context_passes_none(self):
        from jarvis_command_sdk import JarvisSecret
        from jarvis_command_sdk.context import set_current_user_id

        command = MagicMock()
        command.required_secrets = [
            JarvisSecret("USER_TOKEN", "User token", "user", "string"),
        ]

        set_current_user_id(None)
        with patch("services.secret_service.get_secret_value", return_value=None) as mock_get:
            result = _build_secrets(command)

            mock_get.assert_called_once_with("USER_TOKEN", "user", user_id=None)
            assert result == {}
