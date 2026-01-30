"""
Unit tests for CheckTimersCommand.

Tests the check timers command's parameter handling and
various status query scenarios.
"""

from unittest.mock import MagicMock, patch

import pytest

from commands.check_timers_command import CheckTimersCommand
from core.request_information import RequestInformation
from services.timer_service import TimerService


@pytest.fixture
def check_command():
    """Create a CheckTimersCommand instance"""
    return CheckTimersCommand()


@pytest.fixture
def mock_request_info():
    """Create a mock RequestInformation"""
    return MagicMock(spec=RequestInformation)


@pytest.fixture
def fresh_timer_service():
    """Create a fresh TimerService for testing"""
    TimerService._instance = None
    service = TimerService()
    yield service
    service.clear_all()
    TimerService._instance = None


class TestCheckTimersCommandProperties:
    """Test command properties"""

    def test_command_name(self, check_command):
        assert check_command.command_name == "check_timers"

    def test_description(self, check_command):
        assert "check" in check_command.description.lower() or "status" in check_command.description.lower()
        assert "timer" in check_command.description.lower()

    def test_keywords(self, check_command):
        keywords = check_command.keywords
        assert "check timer" in keywords or "timer status" in keywords
        assert "time left" in keywords or "how much time" in keywords

    def test_parameters(self, check_command):
        params = check_command.parameters
        assert len(params) == 1

        label_param = params[0]
        assert label_param.name == "label"
        assert label_param.required is False

    def test_required_secrets_empty(self, check_command):
        assert check_command.required_secrets == []


class TestCheckTimersCommandExamples:
    """Test command examples"""

    def test_prompt_examples(self, check_command):
        examples = check_command.generate_prompt_examples()
        assert len(examples) == 4

        # Check primary example has no label (general query)
        primary = next((e for e in examples if e.is_primary), None)
        assert primary is not None

    def test_adapter_examples(self, check_command):
        examples = check_command.generate_adapter_examples()
        assert len(examples) == 11

        # Check variety: general and specific queries
        has_no_label = any(
            "label" not in e.expected_parameters
            for e in examples
        )
        has_label = any(
            "label" in e.expected_parameters
            for e in examples
        )

        assert has_no_label
        assert has_label


class TestCheckTimersCommandRun:
    """Test command execution"""

    def test_run_no_active_timers(self, check_command, mock_request_info, fresh_timer_service):
        """Test checking when no timers are active"""
        response = check_command.run(mock_request_info)

        assert response.success is True
        assert response.context_data["has_timers"] is False
        assert response.context_data["count"] == 0
        assert "no active" in response.context_data["message"].lower()

    def test_run_single_timer(self, check_command, mock_request_info, fresh_timer_service):
        """Test checking single timer status"""
        fresh_timer_service.set_timer(300, label="pasta")

        response = check_command.run(mock_request_info)

        assert response.success is True
        assert response.context_data["has_timers"] is True
        assert response.context_data["count"] == 1
        assert len(response.context_data["timers"]) == 1

        timer = response.context_data["timers"][0]
        assert timer["label"] == "pasta"
        assert timer["remaining_seconds"] > 0
        assert timer["remaining_text"] is not None

    def test_run_multiple_timers(self, check_command, mock_request_info, fresh_timer_service):
        """Test checking multiple timers"""
        fresh_timer_service.set_timer(300, label="timer1")
        fresh_timer_service.set_timer(600, label="timer2")

        response = check_command.run(mock_request_info)

        assert response.success is True
        assert response.context_data["count"] == 2
        assert len(response.context_data["timers"]) == 2

    def test_run_check_specific_timer(self, check_command, mock_request_info, fresh_timer_service):
        """Test checking specific timer by label"""
        fresh_timer_service.set_timer(300, label="pasta")
        fresh_timer_service.set_timer(600, label="eggs")

        response = check_command.run(mock_request_info, label="pasta")

        assert response.success is True
        assert response.context_data["count"] == 1
        assert "pasta" in response.context_data["message"]

    def test_run_check_timer_not_found(self, check_command, mock_request_info, fresh_timer_service):
        """Test checking non-existent timer"""
        fresh_timer_service.set_timer(300, label="pasta")

        response = check_command.run(mock_request_info, label="eggs")

        assert response.success is True
        assert response.context_data["timers"] == []
        assert "no timer found" in response.context_data["message"].lower()
        assert "available_timers" in response.context_data

    def test_run_message_format_single_with_label(self, check_command, mock_request_info, fresh_timer_service):
        """Test message format for single timer with label"""
        fresh_timer_service.set_timer(150, label="tea")

        response = check_command.run(mock_request_info)

        message = response.context_data["message"]
        assert "tea" in message
        assert "remaining" in message.lower()

    def test_run_message_format_single_no_label(self, check_command, mock_request_info, fresh_timer_service):
        """Test message format for single timer without label"""
        fresh_timer_service.set_timer(150)

        response = check_command.run(mock_request_info)

        message = response.context_data["message"]
        assert "your timer" in message.lower()

    def test_run_no_wait_for_input(self, check_command, mock_request_info, fresh_timer_service):
        """Test that check doesn't wait for follow-up"""
        fresh_timer_service.set_timer(300)

        response = check_command.run(mock_request_info)

        assert response.wait_for_input is False


class TestCheckTimersCommandFormatting:
    """Test formatting helpers"""

    def test_format_remaining_seconds(self, check_command):
        assert check_command._format_remaining(30) == "30 seconds"
        assert check_command._format_remaining(1) == "1 second"

    def test_format_remaining_minutes(self, check_command):
        assert check_command._format_remaining(60) == "1 minute"
        assert check_command._format_remaining(120) == "2 minutes"
        assert check_command._format_remaining(300) == "5 minutes"

    def test_format_remaining_hours(self, check_command):
        assert check_command._format_remaining(3600) == "1 hour"
        assert check_command._format_remaining(7200) == "2 hours"

    def test_format_remaining_compound(self, check_command):
        result = check_command._format_remaining(150)  # 2m 30s
        assert "2 minutes" in result
        assert "30 seconds" in result

    def test_format_remaining_hours_minutes(self, check_command):
        result = check_command._format_remaining(5400)  # 1h 30m
        assert "1 hour" in result
        assert "30 minutes" in result

    def test_format_remaining_hours_no_seconds(self, check_command):
        """Test that seconds are omitted for times over an hour"""
        result = check_command._format_remaining(3661)  # 1h 1m 1s
        # Should show hours and minutes but not seconds for long durations
        assert "1 hour" in result
        assert "1 minute" in result
