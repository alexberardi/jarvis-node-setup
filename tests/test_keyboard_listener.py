"""
Unit tests for keyboard_listener.py.

Tests exit phrase handling, empty input, validation handler,
and multi-turn conversation state management.
"""

from unittest.mock import MagicMock, patch, call

import pytest

from clients.responses.jarvis_command_center import ValidationRequest


class TestKeyboardValidationHandler:
    """Test the keyboard_validation_handler function."""

    def test_prints_question(self, capsys):
        from scripts.keyboard_listener import keyboard_validation_handler

        validation = ValidationRequest(
            question="Which city?",
            parameter_name="city",
            options=["New York", "London"],
            tool_call_id="tc-1",
        )

        with patch("builtins.input", return_value="New York"):
            result = keyboard_validation_handler(validation)

        assert result == "New York"
        captured = capsys.readouterr()
        assert "Which city?" in captured.out

    def test_prints_options(self, capsys):
        from scripts.keyboard_listener import keyboard_validation_handler

        validation = ValidationRequest(
            question="Pick a color",
            parameter_name="color",
            options=["Red", "Blue", "Green"],
            tool_call_id="tc-2",
        )

        with patch("builtins.input", return_value="Blue"):
            keyboard_validation_handler(validation)

        captured = capsys.readouterr()
        assert "Red" in captured.out
        assert "Blue" in captured.out
        assert "Green" in captured.out

    def test_returns_empty_on_eof(self):
        from scripts.keyboard_listener import keyboard_validation_handler

        validation = ValidationRequest(
            question="Question?",
            parameter_name="param",
            options=None,
            tool_call_id="tc-3",
        )

        with patch("builtins.input", side_effect=EOFError):
            result = keyboard_validation_handler(validation)

        assert result == ""


class TestExitPhrases:
    """Test that exit phrases terminate the loop."""

    @pytest.mark.parametrize("phrase", ["quit", "exit", "bye"])
    def test_exit_phrase_stops_loop(self, phrase, capsys):
        from scripts.keyboard_listener import main, EXIT_PHRASES

        assert phrase in EXIT_PHRASES

        with patch("builtins.input", side_effect=[phrase]):
            with patch("scripts.keyboard_listener.CommandExecutionService"):
                main()

        captured = capsys.readouterr()
        assert "Goodbye!" in captured.out

    def test_ctrl_c_stops_loop(self, capsys):
        from scripts.keyboard_listener import main

        with patch("builtins.input", side_effect=KeyboardInterrupt):
            with patch("scripts.keyboard_listener.CommandExecutionService"):
                main()

        captured = capsys.readouterr()
        assert "Goodbye!" in captured.out

    def test_eof_stops_loop(self, capsys):
        from scripts.keyboard_listener import main

        with patch("builtins.input", side_effect=EOFError):
            with patch("scripts.keyboard_listener.CommandExecutionService"):
                main()

        captured = capsys.readouterr()
        assert "Goodbye!" in captured.out


class TestEmptyInput:
    """Test empty input handling."""

    def test_empty_input_is_skipped(self, capsys):
        """Empty input should not call the service."""
        from scripts.keyboard_listener import main

        mock_service = MagicMock()

        with patch("builtins.input", side_effect=["", "  ", "quit"]):
            with patch("scripts.keyboard_listener.CommandExecutionService", return_value=mock_service):
                main()

        mock_service.process_voice_command.assert_not_called()
        mock_service.continue_conversation.assert_not_called()


class TestNewCommand:
    """Test /new command to reset conversation."""

    def test_new_command_resets_conversation(self, capsys):
        from scripts.keyboard_listener import main

        mock_service = MagicMock()
        # First command returns wait_for_input, then /new resets, then quit
        mock_service.process_voice_command.return_value = {
            "success": True,
            "message": "Hello!",
            "conversation_id": "conv-123",
            "wait_for_input": True,
            "clear_history": False,
        }

        with patch("builtins.input", side_effect=["hello", "/new", "quit"]):
            with patch("scripts.keyboard_listener.CommandExecutionService", return_value=mock_service):
                main()

        captured = capsys.readouterr()
        assert "fresh conversation" in captured.out.lower()


class TestMultiTurnConversation:
    """Test multi-turn conversation state management."""

    def test_continues_conversation_when_wait_for_input(self):
        from scripts.keyboard_listener import main

        mock_service = MagicMock()

        # First call: returns wait_for_input=True
        mock_service.process_voice_command.return_value = {
            "success": True,
            "message": "Hey there!",
            "conversation_id": "conv-abc",
            "wait_for_input": True,
            "clear_history": False,
        }
        # Second call: continue_conversation, returns final
        mock_service.continue_conversation.return_value = {
            "success": True,
            "message": "Sure thing!",
            "conversation_id": "conv-abc",
            "wait_for_input": False,
            "clear_history": False,
        }

        with patch("builtins.input", side_effect=["hi", "tell me more", "quit"]):
            with patch("scripts.keyboard_listener.CommandExecutionService", return_value=mock_service):
                main()

        # First message uses process_voice_command
        mock_service.process_voice_command.assert_called_once()
        # Follow-up uses continue_conversation with same conversation_id
        mock_service.continue_conversation.assert_called_once_with(
            "conv-abc",
            "tell me more",
            validation_handler=pytest.approx(mock_service.continue_conversation.call_args[1].get("validation_handler"), abs=0) if mock_service.continue_conversation.call_args[1] else "tell me more",
        )

    def test_clears_conversation_on_wait_for_input_false(self):
        from scripts.keyboard_listener import main

        mock_service = MagicMock()

        # Returns wait_for_input=False — conversation should end
        mock_service.process_voice_command.return_value = {
            "success": True,
            "message": "The weather is sunny.",
            "conversation_id": "conv-xyz",
            "wait_for_input": False,
            "clear_history": False,
        }

        with patch("builtins.input", side_effect=["what's the weather", "hello", "quit"]):
            with patch("scripts.keyboard_listener.CommandExecutionService", return_value=mock_service):
                main()

        # Both commands should use process_voice_command (no continue)
        assert mock_service.process_voice_command.call_count == 2
        mock_service.continue_conversation.assert_not_called()

    def test_clear_history_resets_active_conversation(self):
        from scripts.keyboard_listener import main

        mock_service = MagicMock()

        # First: wait_for_input=True, clear_history=True
        mock_service.process_voice_command.return_value = {
            "success": True,
            "message": "Done with that topic.",
            "conversation_id": "conv-111",
            "wait_for_input": True,
            "clear_history": True,
        }

        with patch("builtins.input", side_effect=["hello", "new topic", "quit"]):
            with patch("scripts.keyboard_listener.CommandExecutionService", return_value=mock_service):
                main()

        # clear_history=True means next message starts fresh
        assert mock_service.process_voice_command.call_count == 2
        mock_service.continue_conversation.assert_not_called()
