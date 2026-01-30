"""
Unit tests for TimerService.

Tests the timer service's core functionality including setting timers,
cancellation, and completion callbacks.
"""

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from services.timer_service import TimerService, get_timer_service, initialize_timer_service


@pytest.fixture
def fresh_timer_service():
    """Create a fresh TimerService instance for testing (bypasses singleton)"""
    # Reset singleton for testing
    TimerService._instance = None
    service = TimerService()
    yield service
    # Cleanup: cancel all timers
    service.clear_all()
    TimerService._instance = None


class TestTimerService:
    """Tests for TimerService"""

    def test_singleton_pattern(self, fresh_timer_service):
        """Test that TimerService follows singleton pattern"""
        service1 = get_timer_service()
        service2 = get_timer_service()
        assert service1 is service2

    def test_set_timer_returns_id(self, fresh_timer_service):
        """Test that set_timer returns a timer ID"""
        timer_id = fresh_timer_service.set_timer(60)
        assert timer_id is not None
        assert isinstance(timer_id, str)
        assert len(timer_id) == 8  # Short UUID

    def test_set_timer_with_label(self, fresh_timer_service):
        """Test setting a timer with a label"""
        timer_id = fresh_timer_service.set_timer(60, label="pasta")
        timer_info = fresh_timer_service.get_timer(timer_id)
        assert timer_info is not None
        assert timer_info["label"] == "pasta"

    def test_get_active_timers(self, fresh_timer_service):
        """Test getting list of active timers"""
        fresh_timer_service.set_timer(60, label="timer1")
        fresh_timer_service.set_timer(120, label="timer2")

        timers = fresh_timer_service.get_active_timers()
        assert len(timers) == 2

        labels = {t["label"] for t in timers}
        assert labels == {"timer1", "timer2"}

    def test_cancel_timer(self, fresh_timer_service):
        """Test cancelling a timer"""
        timer_id = fresh_timer_service.set_timer(60)
        assert len(fresh_timer_service.get_active_timers()) == 1

        result = fresh_timer_service.cancel_timer(timer_id)
        assert result is True
        assert len(fresh_timer_service.get_active_timers()) == 0

    def test_cancel_nonexistent_timer(self, fresh_timer_service):
        """Test cancelling a timer that doesn't exist"""
        result = fresh_timer_service.cancel_timer("nonexistent")
        assert result is False

    def test_clear_all(self, fresh_timer_service):
        """Test clearing all timers"""
        fresh_timer_service.set_timer(60)
        fresh_timer_service.set_timer(120)
        fresh_timer_service.set_timer(180)

        count = fresh_timer_service.clear_all()
        assert count == 3
        assert len(fresh_timer_service.get_active_timers()) == 0

    def test_timer_completion_callback(self, fresh_timer_service):
        """Test that completion callback is invoked when timer fires"""
        callback = MagicMock()
        fresh_timer_service.set_on_complete_callback(callback)

        # Set a very short timer
        timer_id = fresh_timer_service.set_timer(1, label="test")

        # Wait for timer to complete
        time.sleep(1.5)

        callback.assert_called_once()
        call_args = callback.call_args[0]
        assert call_args[0] == timer_id
        assert call_args[1] == "test"

    def test_timer_removed_after_completion(self, fresh_timer_service):
        """Test that timer is removed from active list after completion"""
        fresh_timer_service.set_timer(1)
        assert len(fresh_timer_service.get_active_timers()) == 1

        time.sleep(1.5)
        assert len(fresh_timer_service.get_active_timers()) == 0

    def test_time_remaining(self, fresh_timer_service):
        """Test that remaining time is calculated correctly"""
        timer_id = fresh_timer_service.set_timer(10)
        timer_info = fresh_timer_service.get_timer(timer_id)

        # Should be close to 10 seconds
        assert 9 <= timer_info["remaining_seconds"] <= 10

        time.sleep(1)
        timer_info = fresh_timer_service.get_timer(timer_id)
        assert 8 <= timer_info["remaining_seconds"] <= 9

    def test_get_timer_nonexistent(self, fresh_timer_service):
        """Test getting a timer that doesn't exist"""
        result = fresh_timer_service.get_timer("nonexistent")
        assert result is None

    def test_multiple_concurrent_timers(self, fresh_timer_service):
        """Test multiple timers running concurrently"""
        callback = MagicMock()
        fresh_timer_service.set_on_complete_callback(callback)

        # Set timers with different durations
        fresh_timer_service.set_timer(1, label="first")
        fresh_timer_service.set_timer(2, label="second")

        # After 1.5 seconds, first should have fired
        time.sleep(1.5)
        assert callback.call_count == 1
        assert callback.call_args[0][1] == "first"

        # After another 1 second, second should have fired
        time.sleep(1)
        assert callback.call_count == 2

    def test_cancelled_timer_no_callback(self, fresh_timer_service):
        """Test that cancelled timer doesn't invoke callback"""
        callback = MagicMock()
        fresh_timer_service.set_on_complete_callback(callback)

        timer_id = fresh_timer_service.set_timer(1)
        fresh_timer_service.cancel_timer(timer_id)

        time.sleep(1.5)
        callback.assert_not_called()


class TestTimerServiceInitialization:
    """Tests for timer service initialization"""

    def test_initialize_sets_tts_callback(self):
        """Test that initialize_timer_service sets up TTS callback"""
        # Reset singleton
        TimerService._instance = None

        with patch("services.timer_service._default_timer_complete_handler") as mock_handler:
            service = initialize_timer_service()
            assert service._on_complete_callback is not None

        # Cleanup
        service.clear_all()
        TimerService._instance = None


class TestDefaultTimerCompleteHandler:
    """Tests for the default TTS completion handler"""

    def test_handler_calls_tts_with_label(self):
        """Test that handler calls TTS with label message"""
        from services.timer_service import _default_timer_complete_handler

        mock_tts = MagicMock()
        with patch("core.helpers.get_tts_provider", return_value=mock_tts):
            _default_timer_complete_handler("abc123", "pasta")

        mock_tts.speak.assert_called_once_with(True, "Your pasta timer is done!")

    def test_handler_calls_tts_without_label(self):
        """Test that handler calls TTS without label message"""
        from services.timer_service import _default_timer_complete_handler

        mock_tts = MagicMock()
        with patch("core.helpers.get_tts_provider", return_value=mock_tts):
            _default_timer_complete_handler("abc123", None)

        mock_tts.speak.assert_called_once_with(True, "Your timer is done!")

    def test_handler_handles_tts_error(self):
        """Test that handler gracefully handles TTS errors"""
        from services.timer_service import _default_timer_complete_handler

        with patch("core.helpers.get_tts_provider") as mock_get_tts:
            mock_get_tts.side_effect = Exception("TTS not configured")
            # Should not raise
            _default_timer_complete_handler("abc123", None)
