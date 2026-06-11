"""Tests for the alert announcer — TTS for high-priority queued alerts.

The main wake loop calls :func:`drain_alert_announcements` during quiet
moments (no wake fired yet, ring buffer otherwise idle). The function
checks the alert queue, speaks any high-priority alerts via TTS,
listens briefly for a response (snooze/dismiss/silence), routes the
response through CC, then removes the announced alerts from the queue
(unspoken low-priority alerts stay queued for "what's up"').
Self-contained — depends only on the alert queue service, the TTS
provider, and the same ``listen_for_follow_up`` + CC pieces the main
loop uses.

Coverage:

  * Empty queue / no pending → returns False, no TTS.
  * Only low-priority alerts → returns False (filtered out).
  * High-priority alert spoken, then silent → returns True, no CC call.
  * High-priority alert spoken, response captured → CC routed.
  * TTS raises during speak → continue with next alert; don't crash.
  * Inline listen raises → swallowed.
  * Only the announced alerts are removed — low-priority survive.
  * Queue removal exception → swallowed.
  * Queue service exception at top → returns False.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from core import alert_announcer
from core.ijarvis_speech_to_text_provider import TranscriptionResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeAlert:
    """Stand-in for jarvis_command_sdk.Alert with just the fields the
    announcer reads."""

    def __init__(self, title: str, summary: str, priority: int) -> None:
        self.title = title
        self.summary = summary
        self.priority = priority
        self.id = str(uuid.uuid4())


def _queue_returning(*alerts) -> MagicMock:
    """Build a fake alert queue service. ``get_pending`` returns the
    given alerts; ``remove_ids`` is a no-op MagicMock."""
    q = MagicMock()
    q.get_pending = MagicMock(return_value=list(alerts))
    q.remove_ids = MagicMock()
    return q


def _tts() -> MagicMock:
    tts = MagicMock()
    tts.speak = MagicMock()
    return tts


def _cs() -> MagicMock:
    cs = MagicMock()
    cs.process_voice_command = MagicMock(return_value={"reply": "ok"})
    cs.speak_result = MagicMock()
    return cs


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Per-test: provide a default no-op listen_for_follow_up that
    returns None (silence). Tests that exercise the response path
    override this with their own."""
    monkeypatch.setattr(
        alert_announcer, "listen_for_follow_up",
        lambda *a, **kw: None,
    )


# ---------------------------------------------------------------------------
# Empty / filtered queue → no-op
# ---------------------------------------------------------------------------


class TestEmptyOrFilteredQueue:

    def test_empty_queue_returns_false_and_does_not_speak(self, monkeypatch):
        q = _queue_returning()
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        tts = _tts()
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: tts)

        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )

        assert result is False
        tts.speak.assert_not_called()
        q.remove_ids.assert_not_called()

    def test_only_low_priority_returns_false(self, monkeypatch):
        # Priority 1 (news) is below the ALERT_ANNOUNCE_PRIORITY=3 threshold.
        q = _queue_returning(FakeAlert("Headline", "An article", priority=1))
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        tts = _tts()
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: tts)

        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )

        assert result is False
        tts.speak.assert_not_called()
        q.remove_ids.assert_not_called()

    def test_queue_service_exception_returns_false(self, monkeypatch):
        def boom():
            raise RuntimeError("queue service down")
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", boom)
        # Must not raise.
        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )
        assert result is False


# ---------------------------------------------------------------------------
# High-priority announcement → TTS + flush
# ---------------------------------------------------------------------------


class TestAnnouncement:

    def test_high_priority_alert_announced_and_returns_true(self, monkeypatch):
        # Priority 3 hits the announce threshold.
        reminder = FakeAlert("Reminder", "Take your meds", priority=3)
        q = _queue_returning(reminder)
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        tts = _tts()
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: tts)

        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )

        assert result is True
        tts.speak.assert_called_once_with(True, "Take your meds")
        q.remove_ids.assert_called_once_with([reminder.id])

    def test_multiple_alerts_each_announced(self, monkeypatch):
        q = _queue_returning(
            FakeAlert("A", "first", priority=3),
            FakeAlert("B", "second", priority=3),
        )
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        tts = _tts()
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: tts)

        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )

        assert result is True
        assert tts.speak.call_count == 2

    def test_mixed_priorities_filters_low(self, monkeypatch):
        # Priority 1 stays queued (for "what's up"); priority 3 announced
        # and removed.
        news = FakeAlert("news", "skip me", priority=1)
        rem = FakeAlert("rem", "speak me", priority=3)
        q = _queue_returning(news, rem)
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        tts = _tts()
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: tts)

        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )

        assert result is True
        tts.speak.assert_called_once_with(True, "speak me")
        # Only the announced alert is removed — the news alert survives
        # in the queue (it never lights the LED, but "what's up" can
        # still retrieve it).
        q.remove_ids.assert_called_once_with([rem.id])
        q.flush.assert_not_called()


# ---------------------------------------------------------------------------
# TTS failure recovery
# ---------------------------------------------------------------------------


class TestTtsFailure:

    def test_tts_speak_failure_continues_with_next_alert(self, monkeypatch):
        q = _queue_returning(
            FakeAlert("crash", "first (will fail)", priority=3),
            FakeAlert("ok", "second (should still speak)", priority=3),
        )
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        tts = MagicMock()
        # Fail the first speak, succeed the second.
        tts.speak = MagicMock(side_effect=[RuntimeError("tts down"), None])
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: tts)

        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )

        assert result is True
        # Both speak calls attempted.
        assert tts.speak.call_count == 2
        # Removal still ran (failed-speak alerts are dropped too, not
        # retried — a flaky TTS must not cause an announcement storm).
        q.remove_ids.assert_called_once()


# ---------------------------------------------------------------------------
# Response handling — silence vs. captured response
# ---------------------------------------------------------------------------


class TestResponseHandling:

    def test_silence_after_announce_does_not_route_to_cc(self, monkeypatch):
        # listen_for_follow_up returns None (the autouse fixture default).
        q = _queue_returning(FakeAlert("rem", "speak me", priority=3))
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: _tts())
        cs = _cs()
        stt = MagicMock()
        # If stt is called, the test fails — silence path must short-circuit.
        stt.transcribe_with_speaker = MagicMock(
            side_effect=AssertionError("STT should not be called on silence"),
        )

        alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )

        cs.process_voice_command.assert_not_called()

    def test_captured_response_routed_through_cc(self, monkeypatch):
        # Inline listen captures audio → STT → CC.
        q = _queue_returning(FakeAlert("rem", "Take meds", priority=3))
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: _tts())
        monkeypatch.setattr(
            alert_announcer, "listen_for_follow_up",
            lambda *a, **kw: "/tmp/resp.wav",
        )
        stt = MagicMock()
        stt.transcribe_with_speaker = MagicMock(
            return_value=TranscriptionResult(text="snooze for 20 minutes", speaker_user_id=42),
        )
        cs = _cs()

        alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )

        cs.process_voice_command.assert_called_once()
        # The text passed to CC is the captured response.
        args, kwargs = cs.process_voice_command.call_args
        assert args[0] == "snooze for 20 minutes"
        assert kwargs.get("speaker_user_id") == 42
        cs.speak_result.assert_called_once()

    def test_empty_transcription_does_not_route_to_cc(self, monkeypatch):
        q = _queue_returning(FakeAlert("rem", "Take meds", priority=3))
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: _tts())
        monkeypatch.setattr(
            alert_announcer, "listen_for_follow_up",
            lambda *a, **kw: "/tmp/resp.wav",
        )
        stt = MagicMock()
        stt.transcribe_with_speaker = MagicMock(
            return_value=TranscriptionResult(text=""),
        )
        cs = _cs()

        alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
        )

        cs.process_voice_command.assert_not_called()

    def test_inline_listen_exception_is_swallowed(self, monkeypatch):
        q = _queue_returning(FakeAlert("rem", "Take meds", priority=3))
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: _tts())

        def boom(*a, **kw):
            raise RuntimeError("bus dead")
        monkeypatch.setattr(alert_announcer, "listen_for_follow_up", boom)

        # Must not raise.
        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )
        assert result is True


# ---------------------------------------------------------------------------
# Queue removal failure swallowed
# ---------------------------------------------------------------------------


class TestHasPendingHighPriority:
    """The cheap probe the wake loop polls every ~5 s."""

    def test_empty_queue_returns_false(self, monkeypatch):
        q = _queue_returning()
        monkeypatch.setattr(
            alert_announcer, "get_alert_queue_service", lambda: q,
        )
        assert alert_announcer.has_pending_high_priority_alerts() is False

    def test_only_low_priority_returns_false(self, monkeypatch):
        q = _queue_returning(FakeAlert("Headline", "summary", priority=1))
        monkeypatch.setattr(
            alert_announcer, "get_alert_queue_service", lambda: q,
        )
        assert alert_announcer.has_pending_high_priority_alerts() is False

    def test_high_priority_returns_true(self, monkeypatch):
        q = _queue_returning(FakeAlert("Reminder", "summary", priority=3))
        monkeypatch.setattr(
            alert_announcer, "get_alert_queue_service", lambda: q,
        )
        assert alert_announcer.has_pending_high_priority_alerts() is True

    def test_mixed_priorities_returns_true(self, monkeypatch):
        q = _queue_returning(
            FakeAlert("News", "summary", priority=1),
            FakeAlert("Urgent email", "summary", priority=4),
        )
        monkeypatch.setattr(
            alert_announcer, "get_alert_queue_service", lambda: q,
        )
        assert alert_announcer.has_pending_high_priority_alerts() is True

    def test_queue_service_exception_returns_false(self, monkeypatch):
        def boom():
            raise RuntimeError("queue down")
        monkeypatch.setattr(
            alert_announcer, "get_alert_queue_service", boom,
        )
        # Must not raise — the wake loop calls this every ~5 s and a
        # queue blip must not crash it.
        assert alert_announcer.has_pending_high_priority_alerts() is False


class TestRemovalFailure:

    def test_remove_ids_exception_swallowed(self, monkeypatch):
        q = _queue_returning(FakeAlert("rem", "speak me", priority=3))
        q.remove_ids = MagicMock(side_effect=OSError("queue write failed"))
        monkeypatch.setattr(alert_announcer, "get_alert_queue_service", lambda: q)
        monkeypatch.setattr(alert_announcer, "get_tts_provider", lambda: _tts())

        # Must not raise.
        result = alert_announcer.drain_alert_announcements(
            bus=MagicMock(),
            command_service=_cs(),
            stt_provider=MagicMock(),
            validation_handler=lambda v: "",
        )
        assert result is True
