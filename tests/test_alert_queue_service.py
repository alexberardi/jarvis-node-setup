"""Tests for AlertQueueService."""

import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.alert import Alert
from services.alert_queue_service import AlertQueueService


def _make_alert(
    title: str = "Test alert",
    priority: int = 2,
    ttl_seconds: int = 3600,
    source: str = "test_agent",
) -> Alert:
    now = datetime.now(timezone.utc)
    return Alert(
        source_agent=source,
        title=title,
        summary=f"Summary for {title}",
        created_at=now,
        expires_at=now + timedelta(seconds=ttl_seconds),
        priority=priority,
    )


def _make_expired_alert(title: str = "Expired") -> Alert:
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    return Alert(
        source_agent="test",
        title=title,
        summary="Old",
        created_at=past,
        expires_at=past + timedelta(hours=1),  # expired 1 hour ago
        priority=2,
    )


class TestAlertQueueService:
    def setup_method(self) -> None:
        self.queue = AlertQueueService()

    def test_add_and_count(self) -> None:
        self.queue.add_alert(_make_alert("Alert 1"))
        self.queue.add_alert(_make_alert("Alert 2"))
        assert self.queue.count() == 2

    def test_dedup_by_title_case_insensitive(self) -> None:
        self.queue.add_alert(_make_alert("Breaking News"))
        self.queue.add_alert(_make_alert("breaking news"))
        self.queue.add_alert(_make_alert("BREAKING NEWS"))
        assert self.queue.count() == 1

    def test_expired_alerts_not_counted(self) -> None:
        self.queue.add_alert(_make_alert("Active"))
        self.queue.add_alert(_make_expired_alert("Old"))
        assert self.queue.count() == 1

    def test_get_pending_filters_expired(self) -> None:
        self.queue.add_alert(_make_alert("Active"))
        self.queue.add_alert(_make_expired_alert("Old"))
        pending = self.queue.get_pending()
        assert len(pending) == 1
        assert pending[0].title == "Active"

    def test_get_pending_sorted_by_priority_desc(self) -> None:
        self.queue.add_alert(_make_alert("Low", priority=1))
        self.queue.add_alert(_make_alert("High", priority=3))
        self.queue.add_alert(_make_alert("Medium", priority=2))
        pending = self.queue.get_pending()
        assert [a.title for a in pending] == ["High", "Medium", "Low"]

    def test_flush_returns_and_clears(self) -> None:
        self.queue.add_alert(_make_alert("Alert 1"))
        self.queue.add_alert(_make_alert("Alert 2"))
        flushed = self.queue.flush()
        assert len(flushed) == 2
        assert self.queue.count() == 0

    def test_flush_filters_expired(self) -> None:
        self.queue.add_alert(_make_alert("Active"))
        self.queue.add_alert(_make_expired_alert("Old"))
        flushed = self.queue.flush()
        assert len(flushed) == 1
        assert flushed[0].title == "Active"

    def test_cap_at_max(self) -> None:
        for i in range(60):
            self.queue.add_alert(_make_alert(f"Alert {i}"))
        assert self.queue.count() <= 50

    def test_on_change_called_on_add(self) -> None:
        counts: list[int] = []
        self.queue.on_change = lambda c: counts.append(c)
        self.queue.add_alert(_make_alert("Alert 1"))
        assert counts == [1]

    def test_on_change_called_on_flush(self) -> None:
        self.queue.add_alert(_make_alert("Alert 1"))
        counts: list[int] = []
        self.queue.on_change = lambda c: counts.append(c)
        self.queue.flush()
        assert counts == [0]

    def test_on_change_not_called_for_duplicate(self) -> None:
        self.queue.add_alert(_make_alert("Alert 1"))
        counts: list[int] = []
        self.queue.on_change = lambda c: counts.append(c)
        self.queue.add_alert(_make_alert("Alert 1"))  # duplicate
        assert counts == []

    def test_thread_safety(self) -> None:
        """Concurrent adds from multiple threads should not lose alerts."""
        errors: list[str] = []

        def add_alerts(start: int) -> None:
            try:
                for i in range(20):
                    self.queue.add_alert(_make_alert(f"Thread-{start}-{i}"))
            except Exception as e:
                errors.append(str(e))

        threads = [threading.Thread(target=add_alerts, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        # 5 threads x 20 alerts = 100 unique titles, capped at 50
        assert self.queue.count() <= 50
        assert self.queue.count() > 0


def _install_fake_tts(monkeypatch) -> MagicMock:
    """Inject a fake core.helpers.get_tts_provider so announce_pending_and_flush
    can run without a real TTS provider configured."""
    tts = MagicMock()
    fake_helpers = types.ModuleType("core.helpers")
    fake_helpers.get_tts_provider = lambda: tts  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.helpers", fake_helpers)
    return tts


class TestAnnouncePendingAndFlush:
    def setup_method(self) -> None:
        self.queue = AlertQueueService()

    def test_speaks_each_pending_alert_and_flushes(self, monkeypatch) -> None:
        tts = _install_fake_tts(monkeypatch)
        self.queue.add_alert(_make_alert("Alert 1"))
        self.queue.add_alert(_make_alert("Alert 2"))

        count = self.queue.announce_pending_and_flush()

        assert count == 2
        # Both alerts spoken in priority order (same priority → created_at).
        spoken = [call.args[1] for call in tts.speak.call_args_list]
        assert "Summary for Alert 1" in spoken
        assert "Summary for Alert 2" in spoken
        # Queue is empty after.
        assert self.queue.count() == 0

    def test_speaks_empty_message_when_no_alerts(self, monkeypatch) -> None:
        tts = _install_fake_tts(monkeypatch)
        count = self.queue.announce_pending_and_flush()
        assert count == 0
        tts.speak.assert_called_once()
        assert "no new notifications" in tts.speak.call_args.args[1].lower()

    def test_uses_led_service_transient(self, monkeypatch) -> None:
        _install_fake_tts(monkeypatch)
        led = MagicMock()
        self.queue.add_alert(_make_alert("Alert"))

        self.queue.announce_pending_and_flush(led_service=led)

        # speaking pattern set, then cleared.
        led.set_transient_pattern.assert_any_call("speaking")
        led.set_transient_pattern.assert_any_call(None)

    def test_clears_led_even_when_tts_raises(self, monkeypatch) -> None:
        tts = _install_fake_tts(monkeypatch)
        tts.speak.side_effect = RuntimeError("boom")
        led = MagicMock()
        self.queue.add_alert(_make_alert("Alert"))

        # Should not raise — alert TTS failures are caught per-alert.
        self.queue.announce_pending_and_flush(led_service=led)
        # LED transient cleared in finally.
        led.set_transient_pattern.assert_any_call(None)
