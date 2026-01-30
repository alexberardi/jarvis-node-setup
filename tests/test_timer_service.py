"""
Unit tests for TimerService.

Tests the timer service's core functionality including setting timers,
cancellation, completion callbacks, and persistence for restart survival.
"""

import threading
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.timer_service import (
    TIMER_COMMAND_NAME,
    TimerInfo,
    TimerService,
    get_timer_service,
    initialize_timer_service,
)


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


class TestTimerServicePersistence:
    """Tests for timer persistence functionality"""

    @pytest.fixture
    def fresh_timer_service_with_mock_db(self):
        """Create a fresh TimerService with mocked database"""
        TimerService._instance = None
        service = TimerService()
        yield service
        service.clear_all()
        TimerService._instance = None

    def test_set_timer_persists(self, fresh_timer_service_with_mock_db):
        """Test that set_timer persists the timer"""
        service = fresh_timer_service_with_mock_db

        with patch("services.timer_service.SessionLocal") as mock_session_local:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session_local.return_value = mock_session

            with patch("services.timer_service.CommandDataRepository") as mock_repo_class:
                mock_repo = MagicMock()
                mock_repo_class.return_value = mock_repo

                timer_id = service.set_timer(300, label="pasta")

                mock_repo.save.assert_called_once()
                call_args = mock_repo.save.call_args
                assert call_args.kwargs["command_name"] == TIMER_COMMAND_NAME
                assert call_args.kwargs["data_key"] == timer_id

    def test_cancel_timer_deletes_persisted(self, fresh_timer_service_with_mock_db):
        """Test that cancel_timer removes persisted timer"""
        service = fresh_timer_service_with_mock_db

        # First set a timer (with mocked persistence)
        with patch("services.timer_service.SessionLocal"):
            with patch("services.timer_service.CommandDataRepository"):
                timer_id = service.set_timer(300, label="test")

        # Then cancel it and check deletion
        with patch("services.timer_service.SessionLocal") as mock_session_local:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session_local.return_value = mock_session

            with patch("services.timer_service.CommandDataRepository") as mock_repo_class:
                mock_repo = MagicMock()
                mock_repo_class.return_value = mock_repo

                service.cancel_timer(timer_id)

                mock_repo.delete.assert_called_once_with(TIMER_COMMAND_NAME, timer_id)

    def test_clear_all_deletes_persisted(self, fresh_timer_service_with_mock_db):
        """Test that clear_all removes all persisted timers"""
        service = fresh_timer_service_with_mock_db

        # Set some timers
        with patch("services.timer_service.SessionLocal"):
            with patch("services.timer_service.CommandDataRepository"):
                service.set_timer(300)
                service.set_timer(600)

        # Clear all and check deletion
        with patch("services.timer_service.SessionLocal") as mock_session_local:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session_local.return_value = mock_session

            with patch("services.timer_service.CommandDataRepository") as mock_repo_class:
                mock_repo = MagicMock()
                mock_repo_class.return_value = mock_repo

                service.clear_all()

                mock_repo.delete_all.assert_called_once_with(TIMER_COMMAND_NAME)


class TestTimerServiceFindByLabel:
    """Tests for find_timer_by_label"""

    @pytest.fixture
    def fresh_service(self):
        """Create a fresh TimerService for testing"""
        TimerService._instance = None
        service = TimerService()
        yield service
        service.clear_all()
        TimerService._instance = None

    def test_find_by_exact_label(self, fresh_service):
        """Test finding timer by exact label"""
        with patch("services.timer_service.SessionLocal"):
            with patch("services.timer_service.CommandDataRepository"):
                timer_id = fresh_service.set_timer(300, label="pasta")

        result = fresh_service.find_timer_by_label("pasta")
        assert result == timer_id

    def test_find_by_partial_label(self, fresh_service):
        """Test finding timer by partial label match"""
        with patch("services.timer_service.SessionLocal"):
            with patch("services.timer_service.CommandDataRepository"):
                timer_id = fresh_service.set_timer(300, label="pasta cooking")

        result = fresh_service.find_timer_by_label("pasta")
        assert result == timer_id

    def test_find_case_insensitive(self, fresh_service):
        """Test case-insensitive label matching"""
        with patch("services.timer_service.SessionLocal"):
            with patch("services.timer_service.CommandDataRepository"):
                timer_id = fresh_service.set_timer(300, label="Pasta")

        result = fresh_service.find_timer_by_label("pasta")
        assert result == timer_id

    def test_find_not_found(self, fresh_service):
        """Test finding non-existent timer returns None"""
        with patch("services.timer_service.SessionLocal"):
            with patch("services.timer_service.CommandDataRepository"):
                fresh_service.set_timer(300, label="pasta")

        result = fresh_service.find_timer_by_label("eggs")
        assert result is None


class TestTimerServiceRestore:
    """Tests for timer restoration after restart"""

    @pytest.fixture
    def fresh_service(self):
        """Create a fresh TimerService for testing"""
        TimerService._instance = None
        service = TimerService()
        yield service
        service.clear_all()
        TimerService._instance = None

    def test_restore_active_timers(self, fresh_service):
        """Test restoring timers that haven't expired"""
        now = datetime.now(timezone.utc)
        future_time = now + timedelta(minutes=5)

        mock_persisted = [
            {
                "_data_key": "timer1",
                "timer_id": "timer1",
                "label": "pasta",
                "duration_seconds": 300,
                "started_at": now.isoformat(),
                "ends_at": future_time.isoformat(),
            }
        ]

        with patch("services.timer_service.SessionLocal") as mock_session_local:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session_local.return_value = mock_session

            with patch("services.timer_service.CommandDataRepository") as mock_repo_class:
                mock_repo = MagicMock()
                mock_repo.get_all.return_value = mock_persisted
                mock_repo_class.return_value = mock_repo

                count = fresh_service.restore_timers()

        assert count == 1
        assert len(fresh_service.get_active_timers()) == 1

    def test_restore_expired_timers_fires_callback(self, fresh_service):
        """Test that expired timers fire callback immediately"""
        now = datetime.now(timezone.utc)
        past_time = now - timedelta(minutes=5)
        callback = MagicMock()
        fresh_service.set_on_complete_callback(callback)

        mock_persisted = [
            {
                "_data_key": "expired1",
                "timer_id": "expired1",
                "label": "old_pasta",
                "duration_seconds": 300,
                "started_at": (past_time - timedelta(minutes=5)).isoformat(),
                "ends_at": past_time.isoformat(),
            }
        ]

        with patch("services.timer_service.SessionLocal") as mock_session_local:
            mock_session = MagicMock()
            mock_session.__enter__ = MagicMock(return_value=mock_session)
            mock_session.__exit__ = MagicMock(return_value=False)
            mock_session_local.return_value = mock_session

            with patch("services.timer_service.CommandDataRepository") as mock_repo_class:
                mock_repo = MagicMock()
                mock_repo.get_all.return_value = mock_persisted
                mock_repo_class.return_value = mock_repo

                count = fresh_service.restore_timers()

        # Should not count as restored (it fired immediately)
        assert count == 0
        # But callback should have been called
        callback.assert_called_once_with("expired1", "old_pasta")

    def test_restore_handles_db_error(self, fresh_service):
        """Test restore handles database errors gracefully"""
        with patch("services.timer_service.SessionLocal") as mock_session_local:
            mock_session_local.side_effect = Exception("DB error")

            count = fresh_service.restore_timers()

        assert count == 0


class TestTimerInfoSerialization:
    """Tests for TimerInfo serialization"""

    def test_to_dict(self):
        """Test TimerInfo serialization to dict"""
        now = datetime.now(timezone.utc)
        ends = now + timedelta(minutes=5)
        mock_timer = MagicMock()

        info = TimerInfo(
            timer_id="abc123",
            label="pasta",
            duration_seconds=300,
            started_at=now,
            ends_at=ends,
            timer=mock_timer,
        )

        data = info.to_dict()

        assert data["timer_id"] == "abc123"
        assert data["label"] == "pasta"
        assert data["duration_seconds"] == 300
        assert data["started_at"] == now.isoformat()
        assert data["ends_at"] == ends.isoformat()

    def test_from_dict(self):
        """Test TimerInfo deserialization from dict"""
        now = datetime.now(timezone.utc)
        ends = now + timedelta(minutes=5)
        mock_timer = MagicMock()

        data = {
            "timer_id": "abc123",
            "label": "pasta",
            "duration_seconds": 300,
            "started_at": now.isoformat(),
            "ends_at": ends.isoformat(),
        }

        info = TimerInfo.from_dict(data, mock_timer)

        assert info.timer_id == "abc123"
        assert info.label == "pasta"
        assert info.duration_seconds == 300
        assert info.timer == mock_timer
