"""
Unit tests for ChatCommand.

Tests command properties, run() behavior, example generation,
and antipattern definitions.
"""

from typing import List
from unittest.mock import MagicMock

import pytest

from commands.chat_command import ChatCommand
from core.command_response import CommandResponse
from core.ijarvis_command import CommandAntipattern, CommandExample
from core.request_information import RequestInformation


@pytest.fixture
def command():
    return ChatCommand()


@pytest.fixture
def request_info():
    return RequestInformation(
        voice_command="Hey Jarvis, how's it going?",
        conversation_id="test-conversation-123",
    )


class TestChatCommandProperties:
    """Test command metadata properties."""

    def test_command_name(self, command):
        assert command.command_name == "chat"

    def test_description_mentions_conversation(self, command):
        assert "conversation" in command.description.lower() or "chat" in command.description.lower()

    def test_allow_direct_answer_is_true(self, command):
        """Must be True so the LLM can respond directly to casual conversation."""
        assert command.allow_direct_answer is True

    def test_keywords_include_greetings(self, command):
        keywords = command.keywords
        assert "hello" in keywords
        assert "hi" in keywords
        assert "hey" in keywords
        assert "chat" in keywords

    def test_keywords_include_small_talk(self, command):
        keywords = command.keywords
        assert "how are you" in keywords
        assert "what's up" in keywords

    def test_has_message_parameter(self, command):
        params = command.parameters
        assert len(params) == 1
        assert params[0].name == "message"
        assert params[0].required is True
        assert params[0].param_type == "string"

    def test_no_required_secrets(self, command):
        assert command.required_secrets == []

    def test_has_critical_rules(self, command):
        assert len(command.critical_rules) > 0

    def test_critical_rules_mention_casual(self, command):
        rules_text = " ".join(command.critical_rules).lower()
        assert "casual" in rules_text or "conversation" in rules_text or "small talk" in rules_text


class TestChatCommandAntipatterns:
    """Test antipatterns point to correct alternate commands."""

    def test_has_antipatterns(self, command):
        assert len(command.antipatterns) > 0

    def test_antipatterns_include_answer_question(self, command):
        names = [a.command_name for a in command.antipatterns]
        assert "answer_question" in names

    def test_antipatterns_include_search_web(self, command):
        names = [a.command_name for a in command.antipatterns]
        assert "search_web" in names

    def test_antipatterns_include_weather(self, command):
        names = [a.command_name for a in command.antipatterns]
        assert "get_weather" in names


class TestChatCommandRun:
    """Test run() behavior."""

    def test_run_returns_follow_up_response(self, command, request_info):
        result = command.run(request_info, message="Hello there!")
        assert isinstance(result, CommandResponse)
        assert result.success is True
        assert result.wait_for_input is True

    def test_run_includes_message_in_context(self, command, request_info):
        result = command.run(request_info, message="How are you?")
        assert result.context_data["message"] == "How are you?"

    def test_run_with_empty_message_returns_error(self, command, request_info):
        result = command.run(request_info, message="")
        assert result.success is False
        assert result.error_details is not None

    def test_run_with_missing_message_returns_error(self, command, request_info):
        result = command.run(request_info)
        assert result.success is False


class TestChatCommandExamples:
    """Test example generation."""

    def test_prompt_examples_not_empty(self, command):
        examples = command.generate_prompt_examples()
        assert len(examples) >= 3

    def test_prompt_examples_have_one_primary(self, command):
        examples = command.generate_prompt_examples()
        primary_count = sum(1 for ex in examples if ex.is_primary)
        assert primary_count == 1

    def test_prompt_examples_have_message_parameter(self, command):
        examples = command.generate_prompt_examples()
        for ex in examples:
            assert "message" in ex.expected_parameters

    def test_adapter_examples_are_larger_set(self, command):
        prompt_examples = command.generate_prompt_examples()
        adapter_examples = command.generate_adapter_examples()
        assert len(adapter_examples) > len(prompt_examples)

    def test_adapter_examples_have_one_primary(self, command):
        examples = command.generate_adapter_examples()
        primary_count = sum(1 for ex in examples if ex.is_primary)
        assert primary_count == 1

    def test_adapter_examples_have_message_parameter(self, command):
        examples = command.generate_adapter_examples()
        for ex in examples:
            assert "message" in ex.expected_parameters
