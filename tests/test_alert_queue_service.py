"""Tests for AlertQueueService."""

import threading
from datetime import datetime, timedelta, timezone

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
