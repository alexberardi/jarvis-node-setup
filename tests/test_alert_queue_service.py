"""Tests for AlertQueueService.

on_change semantics under test (the LED contract):

  * Fires with the count of *announceable* alerts — non-expired AND
    priority >= ALERT_ANNOUNCE_PRIORITY (3). Low-priority alerts (news
    at 1, calendar proximity at 2) never light the LED.
  * Level signal: every public operation re-delivers the current count
    (the LED's set_pattern dedups), so a dropped/raced callback heals
    on the next operation — including the scheduler's 10s sweep.
  * flush() always reports 0, even on an already-empty queue, so an
    explicit user dismissal can re-sync a diverged LED.
"""

import sys
import threading
import types
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from core.alert import Alert
from services.alert_queue_service import ALERT_ANNOUNCE_PRIORITY, AlertQueueService


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


def _make_expired_alert(title: str = "Expired", priority: int = 2) -> Alert:
    past = datetime.now(timezone.utc) - timedelta(hours=2)
    return Alert(
        source_agent="test",
        title=title,
        summary="Old",
        created_at=past,
        expires_at=past + timedelta(hours=1),  # expired 1 hour ago
        priority=priority,
    )


class TestAlertQueueService:
    def setup_method(self) -> None:
        self.queue = AlertQueueService()

    def test_announce_priority_constant(self) -> None:
        # The LED gating and the announcer both key off this — a silent
        # change here changes what lights the LED.
        assert ALERT_ANNOUNCE_PRIORITY == 3

    def test_add_and_count(self) -> None:
        self.queue.add_alert(_make_alert("Alert 1"))
        self.queue.add_alert(_make_alert("Alert 2"))
        assert self.queue.count() == 2

    def test_dedup_by_title_case_insensitive(self) -> None:
        self.queue.add_alert(_make_alert("Breaking News"))
        self.queue.add_alert(_make_alert("breaking news"))
        self.queue.add_alert(_make_alert("BREAKING NEWS"))
        assert self.queue.count() == 1

    def test_expired_incoming_alert_rejected(self) -> None:
        # A producer handing over a stale Alert object must not enqueue
        # it (it could blip the LED for something nobody can retrieve).
        counts: list[int] = []
        self.queue.on_change = lambda c: counts.append(c)
        self.queue.add_alert(_make_expired_alert("Stale", priority=3))
        assert counts == []
        assert len(self.queue._alerts) == 0

    def test_get_pending_filters_expired(self) -> None:
        self.queue.add_alert(_make_alert("Active"))
        self.queue._alerts.append(_make_expired_alert("Old"))
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
        self.queue._alerts.append(_make_expired_alert("Old"))
        flushed = self.queue.flush()
        assert len(flushed) == 1
        assert flushed[0].title == "Active"

    def test_cap_at_max(self) -> None:
        for i in range(60):
            self.queue.add_alert(_make_alert(f"Alert {i}"))
        assert self.queue.count() <= 50

    def test_add_alert_with_same_title_after_expiry_succeeds(self) -> None:
        # A same-titled expired entry must not block the re-added alert
        # via dedup — eviction-on-write keeps dedup honest. (Injected
        # directly: add_alert rejects expired incoming alerts.)
        self.queue._alerts.append(_make_expired_alert("Daily News"))
        self.queue.add_alert(_make_alert("Daily News"))
        assert self.queue.count() == 1
        pending = self.queue.get_pending()
        assert pending[0].title == "Daily News"
        assert not pending[0].is_expired

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


class TestOnChangeLedContract:
    """on_change = announceable (priority>=3) count, re-delivered on every
    public operation."""

    def setup_method(self) -> None:
        self.queue = AlertQueueService()
        self.counts: list[int] = []
        self.queue.on_change = lambda c: self.counts.append(c)

    def test_low_priority_add_reports_zero(self) -> None:
        # News (1) / calendar proximity (2) must NOT light the LED — the
        # node never announces them, so to the user "no alerts exist".
        self.queue.add_alert(_make_alert("Headline", priority=1))
        self.queue.add_alert(_make_alert("Calendar soon", priority=2))
        assert self.counts == [0, 0]

    def test_high_priority_add_reports_count(self) -> None:
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        assert self.counts == [1]
        self.queue.add_alert(_make_alert("Urgent email", priority=4))
        assert self.counts == [1, 2]

    def test_mixed_reports_only_announceable(self) -> None:
        self.queue.add_alert(_make_alert("Headline", priority=1))
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        assert self.counts == [0, 1]

    def test_duplicate_add_does_not_fire(self) -> None:
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        self.counts.clear()
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        assert self.counts == []

    def test_flush_always_reports_zero_even_when_empty(self) -> None:
        # Idempotent dismissal: button press / "clear my alerts" on an
        # empty queue must still re-sync a diverged LED.
        self.queue.flush()
        assert self.counts == [0]

    def test_flush_reports_zero_when_only_expired(self) -> None:
        # The deployed-code bug this guards against: alert added (LED on),
        # TTL expired silently, user dismisses — the LED must clear even
        # though there were no non-expired alerts to drop.
        self.queue._alerts.append(_make_expired_alert("Old", priority=3))
        flushed = self.queue.flush()
        assert flushed == []
        assert self.counts == [0]

    def test_count_reports_announceable_not_total(self) -> None:
        self.queue.add_alert(_make_alert("Headline", priority=1))
        self.counts.clear()
        assert self.queue.count() == 1  # total non-expired
        assert self.counts == [0]       # but nothing announceable

    def test_get_pending_evicts_expired_and_reports(self) -> None:
        self.queue._alerts.append(_make_expired_alert("Stale", priority=3))
        assert self.queue.get_pending() == []
        assert self.counts == [0]

    def test_expiry_of_high_priority_clears_via_sweep(self) -> None:
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        assert self.counts == [1]
        # Simulate TTL passing.
        self.queue._alerts[0].expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        evicted = self.queue.sweep_expired()
        assert evicted == 1
        assert self.counts == [1, 0]

    def test_sweep_is_a_reconciler_and_fires_every_time(self) -> None:
        # The scheduler calls sweep_expired every ~10s; re-delivering the
        # current count each tick is what self-heals a diverged LED
        # (dropped callback, out-of-order add/flush race).
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        self.counts.clear()
        assert self.queue.sweep_expired() == 0
        assert self.queue.sweep_expired() == 0
        assert self.counts == [1, 1]

    def test_sweep_on_empty_queue_reports_zero(self) -> None:
        assert self.queue.sweep_expired() == 0
        assert self.counts == [0]

    def test_sweep_keeps_reporting_zero_for_low_priority_residents(self) -> None:
        # News can sit in the queue for its whole TTL — the LED must stay
        # dark the entire time.
        self.queue.add_alert(_make_alert("Headline", priority=1))
        self.counts.clear()
        self.queue.sweep_expired()
        assert self.counts == [0]
        assert self.queue.count() == 1

    def test_on_change_exception_swallowed(self) -> None:
        self.queue.on_change = lambda c: (_ for _ in ()).throw(RuntimeError("led dead"))
        # Must not raise.
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        self.queue.flush()


class TestRemoveIds:
    def setup_method(self) -> None:
        self.queue = AlertQueueService()
        self.counts: list[int] = []
        self.queue.on_change = lambda c: self.counts.append(c)

    def test_removes_only_given_ids(self) -> None:
        news = _make_alert("Headline", priority=1)
        rem = _make_alert("Reminder", priority=3)
        self.queue.add_alert(news)
        self.queue.add_alert(rem)
        self.counts.clear()

        removed = self.queue.remove_ids([rem.id])

        assert removed == 1
        # The announced reminder is gone, the news alert survives for
        # "what's up" — and the LED goes dark (no announceable left).
        assert [a.title for a in self.queue.get_pending()] == ["Headline"]
        assert self.counts[0] == 0

    def test_unknown_ids_are_noop(self) -> None:
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        self.counts.clear()
        assert self.queue.remove_ids(["nope"]) == 0
        # Still re-delivers the current count (level signal).
        assert self.counts == [1]

    def test_remove_keeps_led_on_when_announceable_remain(self) -> None:
        rem1 = _make_alert("Reminder 1", priority=3)
        rem2 = _make_alert("Reminder 2", priority=3)
        self.queue.add_alert(rem1)
        self.queue.add_alert(rem2)
        self.counts.clear()
        self.queue.remove_ids([rem1.id])
        assert self.counts == [1]


class TestPriorityAwareDedup:
    def setup_method(self) -> None:
        self.queue = AlertQueueService()
        self.counts: list[int] = []
        self.queue.on_change = lambda c: self.counts.append(c)

    def test_higher_priority_replaces_lower_twin(self) -> None:
        # A priority-3 re-emission must not be masked by its priority-2
        # twin — it may need to light the LED and get announced.
        self.queue.add_alert(_make_alert("Meeting soon", priority=2))
        assert self.counts == [0]
        self.queue.add_alert(_make_alert("Meeting soon", priority=3))
        assert self.counts == [0, 1]
        pending = self.queue.get_pending()
        assert len(pending) == 1
        assert pending[0].priority == 3

    def test_lower_priority_still_deduped(self) -> None:
        self.queue.add_alert(_make_alert("Meeting soon", priority=3))
        self.counts.clear()
        self.queue.add_alert(_make_alert("Meeting soon", priority=2))
        assert self.counts == []
        assert self.queue.get_pending()[0].priority == 3


def _install_fake_tts(monkeypatch) -> MagicMock:
    """Inject a fake core.helpers.get_tts_provider so announce_pending_and_flush
    can run without a real TTS provider configured."""
    tts = MagicMock()
    fake_helpers = types.ModuleType("core.helpers")
    fake_helpers.get_tts_provider = lambda: tts  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.helpers", fake_helpers)
    return tts


def _install_broken_tts(monkeypatch) -> None:
    """get_tts_provider raises — simulates TTS service unavailable."""
    def boom():
        raise RuntimeError("tts service down")
    fake_helpers = types.ModuleType("core.helpers")
    fake_helpers.get_tts_provider = boom  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "core.helpers", fake_helpers)


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

    def test_empty_press_resyncs_led(self, monkeypatch) -> None:
        # The signature stuck state: LED purple, queue empty. The button
        # press must fire on_change(0) so the LED clears instead of
        # "No new notifications" + still-purple.
        _install_fake_tts(monkeypatch)
        counts: list[int] = []
        self.queue.on_change = lambda c: counts.append(c)
        self.queue.announce_pending_and_flush()
        assert 0 in counts

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

    def test_tts_provider_unavailable_keeps_alerts(self, monkeypatch) -> None:
        # Nothing was heard → alerts stay queued; the press shows a brief
        # error pattern instead of silently doing nothing.
        _install_broken_tts(monkeypatch)
        led = MagicMock()
        self.queue.add_alert(_make_alert("Alert", priority=3))

        count = self.queue.announce_pending_and_flush(led_service=led)

        assert count == 0
        assert self.queue.count() == 1
        led.preview_pattern.assert_called_once_with("error", 2.0)

    def test_alert_added_during_announcement_survives(self, monkeypatch) -> None:
        # TTS playback takes seconds; an alert the scheduler adds mid-
        # announcement must NOT be wiped by the post-announce cleanup
        # (the old flush() destroyed it unspoken and unretrievable).
        tts = _install_fake_tts(monkeypatch)
        self.queue.add_alert(_make_alert("Spoken alert"))
        late = _make_alert("Late arrival", priority=3)
        tts.speak.side_effect = lambda *a, **kw: self.queue.add_alert(late)

        spoken = self.queue.announce_pending_and_flush()

        assert spoken == 1
        remaining = self.queue.get_pending()
        assert [a.title for a in remaining] == ["Late arrival"]

    def test_alert_added_during_empty_announcement_survives(self, monkeypatch) -> None:
        # Same race in the empty branch: get_pending() saw nothing, an
        # alert lands before the cleanup — it must survive the "No new
        # notifications." path.
        tts = _install_fake_tts(monkeypatch)
        late = _make_alert("Late arrival", priority=3)
        original_sweep = self.queue.sweep_expired

        def add_then_sweep() -> int:
            self.queue.add_alert(late)
            return original_sweep()

        monkeypatch.setattr(self.queue, "sweep_expired", add_then_sweep)
        spoken = self.queue.announce_pending_and_flush()

        assert spoken == 0
        tts.speak.assert_called_once()
        assert [a.title for a in self.queue.get_pending()] == ["Late arrival"]
