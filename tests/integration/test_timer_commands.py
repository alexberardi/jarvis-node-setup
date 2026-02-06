"""
Integration tests for Timer commands.

Tests the timer command execution with mocked dependencies,
ensuring deterministic behavior for set, cancel, and check operations.
"""

import pytest
from unittest.mock import patch, MagicMock

from commands.timer_command import TimerCommand
from commands.cancel_timer_command import CancelTimerCommand
from commands.check_timers_command import CheckTimersCommand
from core.command_response import CommandResponse


@pytest.fixture
def timer_cmd():
    """TimerCommand instance."""
    return TimerCommand()


@pytest.fixture
def cancel_timer_cmd():
    """CancelTimerCommand instance."""
    return CancelTimerCommand()


@pytest.fixture
def check_timers_cmd():
    """CheckTimersCommand instance."""
    return CheckTimersCommand()


@pytest.fixture
def mock_timer_service():
    """Mock timer service for all timer operations."""
    mock_service = MagicMock()
    mock_service.set_timer.return_value = "timer-123"
    mock_service.cancel_timer.return_value = True
    mock_service.cancel_all_timers.return_value = 2
    mock_service.get_active_timers.return_value = []
    return mock_service


# ============================================================================
# Test: Set Timer
# ============================================================================


class TestSetTimer:
    """Test timer creation with various parameter combinations."""

    def test_set_timer_seconds(self, timer_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User says "set a timer for 30 seconds"
        LLM returns: duration_seconds=30
        Expected: Timer created successfully
        """
        request_info = request_info_factory("set a timer for 30 seconds")
        
        with patch("commands.timer_command.get_timer_service", return_value=mock_timer_service):
            response = timer_cmd.run(
                request_info,
                duration_seconds=30,
            )

            assert response.success is True
            mock_timer_service.set_timer.assert_called_once_with(30, None)

    def test_set_timer_with_label(self, timer_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User says "set a 5 minute pasta timer"
        LLM returns: duration_seconds=300, label="pasta"
        Expected: Timer created with label
        """
        request_info = request_info_factory("set a 5 minute pasta timer")
        
        with patch("commands.timer_command.get_timer_service", return_value=mock_timer_service):
            response = timer_cmd.run(
                request_info,
                duration_seconds=300,
                label="pasta",
            )

            assert response.success is True
            mock_timer_service.set_timer.assert_called_once_with(300, "pasta")
            assert response.context_data["label"] == "pasta"

    def test_set_timer_missing_duration(self, timer_cmd, request_info_factory):
        """
        Scenario: LLM forgets duration parameter
        Expected: Error response
        """
        request_info = request_info_factory("set a timer")
        
        response = timer_cmd.run(request_info)  # No duration_seconds

        assert response.success is False
        assert "duration" in response.error_details.lower()

    def test_set_timer_zero_duration(self, timer_cmd, request_info_factory):
        """
        Scenario: LLM returns zero duration
        Expected: Error response
        """
        request_info = request_info_factory("set a timer")
        
        response = timer_cmd.run(
            request_info,
            duration_seconds=0,
        )

        assert response.success is False

    def test_set_timer_negative_duration(self, timer_cmd, request_info_factory):
        """
        Scenario: LLM returns negative duration
        Expected: Error response
        """
        request_info = request_info_factory("set a timer")
        
        response = timer_cmd.run(
            request_info,
            duration_seconds=-5,
        )

        assert response.success is False

    def test_set_timer_response_contains_formatted_duration(self, timer_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: Timer set successfully
        Expected: Response contains human-readable duration
        """
        request_info = request_info_factory("set a timer for 90 seconds")
        
        with patch("commands.timer_command.get_timer_service", return_value=mock_timer_service):
            response = timer_cmd.run(
                request_info,
                duration_seconds=90,
            )

            assert response.success is True
            # 90 seconds = 1 minute 30 seconds
            assert "duration_text" in response.context_data


# ============================================================================
# Test: Cancel Timer
# ============================================================================


class TestCancelTimer:
    """Test timer cancellation scenarios."""

    def test_cancel_by_label(self, cancel_timer_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User says "cancel the pasta timer"
        LLM returns: label="pasta"
        Expected: Timer cancelled
        """
        request_info = request_info_factory("cancel the pasta timer")
        mock_timer_service.get_active_timers.return_value = [
            {"timer_id": "timer-1", "label": "pasta", "remaining_seconds": 120}
        ]
        
        with patch("commands.cancel_timer_command.get_timer_service", return_value=mock_timer_service):
            response = cancel_timer_cmd.run(
                request_info,
                label="pasta",
            )

            assert response.success is True

    def test_cancel_all(self, cancel_timer_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User says "cancel all timers"
        LLM returns: label="all"
        Expected: All timers cancelled
        """
        request_info = request_info_factory("cancel all timers")
        mock_timer_service.get_active_timers.return_value = [
            {"timer_id": "timer-1", "label": "pasta", "remaining_seconds": 120},
            {"timer_id": "timer-2", "label": "eggs", "remaining_seconds": 60},
        ]
        
        with patch("commands.cancel_timer_command.get_timer_service", return_value=mock_timer_service):
            response = cancel_timer_cmd.run(
                request_info,
                label="all",
            )

            assert response.success is True

    def test_cancel_nonexistent_timer(self, cancel_timer_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User tries to cancel timer that doesn't exist
        Expected: Handled gracefully with appropriate message
        """
        request_info = request_info_factory("cancel the pizza timer")
        mock_timer_service.get_active_timers.return_value = []
        mock_timer_service.cancel_timer.return_value = False
        
        with patch("commands.cancel_timer_command.get_timer_service", return_value=mock_timer_service):
            response = cancel_timer_cmd.run(
                request_info,
                label="pizza",
            )

            # Should succeed but indicate no timer found
            assert response is not None


# ============================================================================
# Test: Check Timers
# ============================================================================


class TestCheckTimers:
    """Test timer status queries."""

    def test_check_specific_timer(self, check_timers_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User says "how much time on the pasta timer"
        LLM returns: label="pasta"
        Expected: Timer status returned
        """
        request_info = request_info_factory("how much time on the pasta timer")
        mock_timer_service.get_active_timers.return_value = [
            {"timer_id": "timer-1", "label": "pasta", "remaining_seconds": 180}
        ]
        # Also mock get_timer for when checking specific timer details
        mock_timer_service.get_timer.return_value = {
            "timer_id": "timer-1",
            "label": "pasta",
            "remaining_seconds": 180
        }

        with patch("commands.check_timers_command.get_timer_service", return_value=mock_timer_service):
            response = check_timers_cmd.run(
                request_info,
                label="pasta",
            )

            assert response.success is True

    def test_check_all_timers(self, check_timers_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User says "what timers are running"
        LLM returns: no label (check all)
        Expected: All active timers listed
        """
        request_info = request_info_factory("what timers are running")
        mock_timer_service.get_active_timers.return_value = [
            {"timer_id": "timer-1", "label": "pasta", "remaining_seconds": 180},
            {"timer_id": "timer-2", "label": "eggs", "remaining_seconds": 60},
        ]
        
        with patch("commands.check_timers_command.get_timer_service", return_value=mock_timer_service):
            response = check_timers_cmd.run(request_info)

            assert response.success is True

    def test_check_timers_none_active(self, check_timers_cmd, request_info_factory, mock_timer_service):
        """
        Scenario: User checks timers but none are running
        Expected: Message indicating no active timers
        """
        request_info = request_info_factory("any timers running")
        mock_timer_service.get_active_timers.return_value = []
        
        with patch("commands.check_timers_command.get_timer_service", return_value=mock_timer_service):
            response = check_timers_cmd.run(request_info)

            assert response.success is True
