"""
Unit tests for TimerCommand.

Tests the timer command's parameter handling, duration formatting,
and integration with the timer service.
"""

from unittest.mock import MagicMock, patch

import pytest

from commands.timer_command import TimerCommand
from core.request_information import RequestInformation
from services.timer_service import TimerService


@pytest.fixture
def timer_command():
    """Create a TimerCommand instance"""
    return TimerCommand()


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


class TestTimerCommandProperties:
    """Test command properties"""

    def test_command_name(self, timer_command):
        assert timer_command.command_name == "set_timer"

    def test_description(self, timer_command):
        assert "timer" in timer_command.description.lower()

    def test_keywords(self, timer_command):
        keywords = timer_command.keywords
        assert "timer" in keywords
        assert "remind" in keywords

    def test_parameters(self, timer_command):
        params = timer_command.parameters
        param_names = [p.name for p in params]
        assert "duration_seconds" in param_names
        assert "label" in param_names

        duration_param = next(p for p in params if p.name == "duration_seconds")
        assert duration_param.required is True

        label_param = next(p for p in params if p.name == "label")
        assert label_param.required is False

    def test_required_secrets_empty(self, timer_command):
        assert timer_command.required_secrets == []


class TestTimerCommandExamples:
    """Test command examples"""

    def test_prompt_examples(self, timer_command):
        examples = timer_command.generate_prompt_examples()
        assert len(examples) == 5

        # Check primary example
        primary = next((e for e in examples if e.is_primary), None)
        assert primary is not None
        assert "duration_seconds" in primary.expected_parameters

    def test_adapter_examples(self, timer_command):
        examples = timer_command.generate_adapter_examples()
        assert len(examples) == 19

        # Check variety of examples
        has_label = any("label" in e.expected_parameters for e in examples)
        assert has_label

        # Check time conversions
        five_min_example = next(
            (e for e in examples if e.expected_parameters.get("duration_seconds") == 300),
            None
        )
        assert five_min_example is not None


class TestTimerCommandRun:
    """Test command execution"""

    def test_run_sets_timer(self, timer_command, mock_request_info, fresh_timer_service):
        """Test successful timer creation"""
        response = timer_command.run(
            mock_request_info,
            duration_seconds=300
        )

        assert response.success is True
        assert response.context_data["duration_seconds"] == 300
        assert response.context_data["timer_id"] is not None
        assert "5 minutes" in response.context_data["duration_text"]

    def test_run_with_label(self, timer_command, mock_request_info, fresh_timer_service):
        """Test timer creation with label"""
        response = timer_command.run(
            mock_request_info,
            duration_seconds=600,
            label="pasta"
        )

        assert response.success is True
        assert response.context_data["label"] == "pasta"
        assert "pasta" in response.context_data["message"]

    def test_run_missing_duration(self, timer_command, mock_request_info):
        """Test error when duration is missing"""
        response = timer_command.run(mock_request_info)

        assert response.success is False
        assert "required" in response.error_details.lower()

    def test_run_invalid_duration_negative(self, timer_command, mock_request_info):
        """Test error for negative duration"""
        response = timer_command.run(
            mock_request_info,
            duration_seconds=-10
        )

        assert response.success is False
        assert "positive" in response.error_details.lower()

    def test_run_invalid_duration_zero(self, timer_command, mock_request_info):
        """Test error for zero duration"""
        response = timer_command.run(
            mock_request_info,
            duration_seconds=0
        )

        assert response.success is False

    def test_run_no_wait_for_input(self, timer_command, mock_request_info, fresh_timer_service):
        """Test that timer doesn't wait for follow-up"""
        response = timer_command.run(
            mock_request_info,
            duration_seconds=60
        )

        assert response.wait_for_input is False


class TestDurationFormatting:
    """Test duration formatting"""

    def test_format_seconds(self, timer_command):
        assert timer_command._format_duration(30) == "30 seconds"
        assert timer_command._format_duration(1) == "1 second"

    def test_format_minutes(self, timer_command):
        assert timer_command._format_duration(60) == "1 minute"
        assert timer_command._format_duration(120) == "2 minutes"
        assert timer_command._format_duration(300) == "5 minutes"

    def test_format_hours(self, timer_command):
        assert timer_command._format_duration(3600) == "1 hour"
        assert timer_command._format_duration(7200) == "2 hours"

    def test_format_compound_minutes_seconds(self, timer_command):
        result = timer_command._format_duration(150)  # 2m 30s
        assert "2 minutes" in result
        assert "30 seconds" in result

    def test_format_compound_hours_minutes(self, timer_command):
        result = timer_command._format_duration(5400)  # 1h 30m
        assert "1 hour" in result
        assert "30 minutes" in result

    def test_format_compound_all_units(self, timer_command):
        result = timer_command._format_duration(3661)  # 1h 1m 1s
        assert "1 hour" in result
        assert "1 minute" in result
        assert "1 second" in result


class TestConfirmationMessage:
    """Test confirmation message building"""

    def test_message_without_label(self, timer_command):
        msg = timer_command._build_confirmation_message("5 minutes", None)
        assert msg == "Timer set for 5 minutes"

    def test_message_with_label(self, timer_command):
        msg = timer_command._build_confirmation_message("10 minutes", "pasta")
        assert msg == "Timer set for 10 minutes for pasta"
