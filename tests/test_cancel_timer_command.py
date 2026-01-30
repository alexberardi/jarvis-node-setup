"""
Unit tests for CancelTimerCommand.

Tests the cancel timer command's parameter handling and
various cancellation scenarios (by label, all, single timer).
"""

from unittest.mock import MagicMock, patch

import pytest

from commands.cancel_timer_command import CancelTimerCommand
from core.request_information import RequestInformation
from services.timer_service import TimerService


@pytest.fixture
def cancel_command():
    """Create a CancelTimerCommand instance"""
    return CancelTimerCommand()


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


class TestCancelTimerCommandProperties:
    """Test command properties"""

    def test_command_name(self, cancel_command):
        assert cancel_command.command_name == "cancel_timer"

    def test_description(self, cancel_command):
        assert "cancel" in cancel_command.description.lower()
        assert "timer" in cancel_command.description.lower()

    def test_keywords(self, cancel_command):
        keywords = cancel_command.keywords
        assert "cancel timer" in keywords
        assert "stop timer" in keywords

    def test_parameters(self, cancel_command):
        params = cancel_command.parameters
        assert len(params) == 1

        label_param = params[0]
        assert label_param.name == "label"
        assert label_param.required is False

    def test_required_secrets_empty(self, cancel_command):
        assert cancel_command.required_secrets == []


class TestCancelTimerCommandExamples:
    """Test command examples"""

    def test_prompt_examples(self, cancel_command):
        examples = cancel_command.generate_prompt_examples()
        assert len(examples) == 4

        # Check primary example has label
        primary = next((e for e in examples if e.is_primary), None)
        assert primary is not None
        assert "label" in primary.expected_parameters

    def test_adapter_examples(self, cancel_command):
        examples = cancel_command.generate_adapter_examples()
        assert len(examples) == 12

        # Check variety: by label, all, no label
        has_specific_label = any(
            e.expected_parameters.get("label") not in [None, "all"]
            for e in examples
        )
        has_all = any(
            e.expected_parameters.get("label") == "all"
            for e in examples
        )
        has_no_label = any(
            "label" not in e.expected_parameters
            for e in examples
        )

        assert has_specific_label
        assert has_all
        assert has_no_label


class TestCancelTimerCommandRun:
    """Test command execution"""

    def test_run_no_active_timers(self, cancel_command, mock_request_info, fresh_timer_service):
        """Test cancelling when no timers are active"""
        response = cancel_command.run(mock_request_info)

        assert response.success is True
        assert response.context_data["cancelled"] is False
        assert "no active" in response.context_data["message"].lower()

    def test_run_cancel_by_label(self, cancel_command, mock_request_info, fresh_timer_service):
        """Test cancelling a timer by label"""
        # Set up a timer with label
        fresh_timer_service.set_timer(300, label="pasta")

        response = cancel_command.run(mock_request_info, label="pasta")

        assert response.success is True
        assert response.context_data["cancelled"] is True
        assert "pasta" in response.context_data["message"]
        assert len(fresh_timer_service.get_active_timers()) == 0

    def test_run_cancel_by_partial_label(self, cancel_command, mock_request_info, fresh_timer_service):
        """Test cancelling a timer by partial label match"""
        fresh_timer_service.set_timer(300, label="pasta cooking")

        response = cancel_command.run(mock_request_info, label="pasta")

        assert response.success is True
        assert response.context_data["cancelled"] is True

    def test_run_cancel_all(self, cancel_command, mock_request_info, fresh_timer_service):
        """Test cancelling all timers"""
        fresh_timer_service.set_timer(300, label="timer1")
        fresh_timer_service.set_timer(600, label="timer2")
        fresh_timer_service.set_timer(900, label="timer3")

        response = cancel_command.run(mock_request_info, label="all")

        assert response.success is True
        assert response.context_data["cancelled"] is True
        assert response.context_data["cancelled_count"] == 3
        assert len(fresh_timer_service.get_active_timers()) == 0

    def test_run_cancel_single_timer_no_label(self, cancel_command, mock_request_info, fresh_timer_service):
        """Test cancelling single timer when no label provided"""
        fresh_timer_service.set_timer(300, label="only_timer")

        response = cancel_command.run(mock_request_info)

        assert response.success is True
        assert response.context_data["cancelled"] is True
        assert len(fresh_timer_service.get_active_timers()) == 0

    def test_run_multiple_timers_no_label_asks_clarification(
        self, cancel_command, mock_request_info, fresh_timer_service
    ):
        """Test that multiple timers without label asks for clarification"""
        fresh_timer_service.set_timer(300, label="timer1")
        fresh_timer_service.set_timer(600, label="timer2")

        response = cancel_command.run(mock_request_info)

        assert response.success is True
        assert response.context_data["cancelled"] is False
        assert response.context_data["needs_clarification"] is True
        assert response.wait_for_input is True

    def test_run_label_not_found(self, cancel_command, mock_request_info, fresh_timer_service):
        """Test cancelling with non-existent label"""
        fresh_timer_service.set_timer(300, label="pasta")

        response = cancel_command.run(mock_request_info, label="eggs")

        assert response.success is True
        assert response.context_data["cancelled"] is False
        assert "no timer found" in response.context_data["message"].lower()
        assert "available_timers" in response.context_data


class TestCancelTimerCommandFormatting:
    """Test formatting helpers"""

    def test_format_remaining_seconds(self, cancel_command):
        assert cancel_command._format_remaining(30) == "30 seconds"
        assert cancel_command._format_remaining(1) == "1 second"

    def test_format_remaining_minutes(self, cancel_command):
        assert cancel_command._format_remaining(60) == "1 minute"
        assert cancel_command._format_remaining(120) == "2 minutes"

    def test_format_remaining_hours_minutes(self, cancel_command):
        result = cancel_command._format_remaining(5400)  # 1h 30m
        assert "1 hour" in result
        assert "30 minutes" in result
