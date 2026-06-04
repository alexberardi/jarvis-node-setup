"""Tests for ReminderService.update_reminder — added for the mobile
command-data browser. Exercises the runtime-state side-effects (in-memory
cache, recurrence-change reset) that a raw repo write would miss."""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from services.reminder_service import ReminderService


@pytest.fixture
def service() -> ReminderService:
    svc = ReminderService()
    svc._storage = MagicMock()
    return svc


@pytest.fixture
def reminder(service: ReminderService):
    due = datetime(2026, 6, 4, 18, 0, tzinfo=timezone.utc)
    return service.create_reminder("take out trash", due, recurrence="daily", user_id=42)


class TestUpdateReminderText:
    def test_basic_text_change(self, service: ReminderService, reminder) -> None:
        updated, error = service.update_reminder(
            reminder.reminder_id, {"text": "take out garbage"}
        )
        assert error is None
        assert updated is not None
        assert updated.text == "take out garbage"
        # In-memory state updated, not just persisted
        assert service.get_reminder(reminder.reminder_id).text == "take out garbage"

    def test_empty_text_rejected(self, service: ReminderService, reminder) -> None:
        _, error = service.update_reminder(reminder.reminder_id, {"text": ""})
        assert error is not None
        assert "non-empty" in error

    def test_whitespace_only_text_rejected(self, service: ReminderService, reminder) -> None:
        _, error = service.update_reminder(reminder.reminder_id, {"text": "   "})
        assert error is not None

    def test_text_trimmed(self, service: ReminderService, reminder) -> None:
        updated, _ = service.update_reminder(
            reminder.reminder_id, {"text": "  spaces  "}
        )
        assert updated.text == "spaces"


class TestUpdateReminderDueAt:
    def test_iso_with_tz(self, service: ReminderService, reminder) -> None:
        new_due = "2026-06-05T19:00:00+00:00"
        updated, error = service.update_reminder(reminder.reminder_id, {"due_at": new_due})
        assert error is None
        parsed = datetime.fromisoformat(updated.due_at)
        assert parsed.year == 2026
        assert parsed.month == 6
        assert parsed.day == 5
        assert parsed.tzinfo is not None

    def test_naive_iso_gets_local_tz(self, service: ReminderService, reminder) -> None:
        updated, error = service.update_reminder(
            reminder.reminder_id, {"due_at": "2026-06-05T19:00:00"}
        )
        assert error is None
        parsed = datetime.fromisoformat(updated.due_at)
        assert parsed.tzinfo is not None

    def test_invalid_due_at_string(self, service: ReminderService, reminder) -> None:
        _, error = service.update_reminder(reminder.reminder_id, {"due_at": "not-a-date"})
        assert error is not None
        assert "ISO 8601" in error

    def test_non_string_due_at(self, service: ReminderService, reminder) -> None:
        _, error = service.update_reminder(reminder.reminder_id, {"due_at": 12345})
        assert error is not None


class TestUpdateReminderRecurrence:
    def test_change_recurrence_resets_fire_state(
        self, service: ReminderService, reminder
    ) -> None:
        # Simulate a previously-fired reminder
        service.mark_announced(reminder.reminder_id)
        r_before = service.get_reminder(reminder.reminder_id)
        assert r_before.announce_count >= 1
        # daily reminders advance on mark_announced rather than getting
        # latched, so seed announced=True manually so the test reflects the
        # "user wants to reset fire history" case the handler cares about.
        r_before.announced = True
        r_before.last_announced_at = datetime.now(timezone.utc).isoformat()
        r_before.snooze_until = "2026-06-05T20:00:00+00:00"

        updated, error = service.update_reminder(
            reminder.reminder_id, {"recurrence": "weekly"}
        )
        assert error is None
        assert updated.recurrence == "weekly"
        assert updated.announced is False
        assert updated.announce_count == 0
        assert updated.last_announced_at is None
        assert updated.snooze_until is None

    def test_same_recurrence_keeps_state(
        self, service: ReminderService, reminder
    ) -> None:
        # Already "daily"; setting to "daily" again shouldn't blow away history.
        service.mark_announced(reminder.reminder_id)
        before = service.get_reminder(reminder.reminder_id)
        count_before = before.announce_count

        updated, error = service.update_reminder(
            reminder.reminder_id, {"recurrence": "daily"}
        )
        assert error is None
        assert updated.announce_count == count_before

    def test_unknown_recurrence_rejected(
        self, service: ReminderService, reminder
    ) -> None:
        _, error = service.update_reminder(
            reminder.reminder_id, {"recurrence": "fortnightly"}
        )
        assert error is not None

    def test_clear_recurrence_to_none(
        self, service: ReminderService, reminder
    ) -> None:
        updated, error = service.update_reminder(
            reminder.reminder_id, {"recurrence": None}
        )
        assert error is None
        assert updated.recurrence is None


class TestUpdateReminderUserScope:
    def test_owner_can_update(self, service: ReminderService, reminder) -> None:
        _, error = service.update_reminder(
            reminder.reminder_id, {"text": "new"}, user_id=42
        )
        assert error is None

    def test_other_user_cannot_update(
        self, service: ReminderService, reminder
    ) -> None:
        _, error = service.update_reminder(
            reminder.reminder_id, {"text": "new"}, user_id=99
        )
        assert error is not None
        assert "not found" in error

    def test_legacy_owner_none_visible_to_all(self, service: ReminderService) -> None:
        due = datetime(2026, 6, 4, 18, 0, tzinfo=timezone.utc)
        legacy = service.create_reminder("legacy", due, user_id=None)
        _, error = service.update_reminder(
            legacy.reminder_id, {"text": "edited"}, user_id=42
        )
        assert error is None

    def test_missing_reminder(self, service: ReminderService) -> None:
        _, error = service.update_reminder("rem_does_not_exist", {"text": "x"})
        assert error is not None


class TestUpdatePersistence:
    def test_persists_after_update(self, service: ReminderService, reminder) -> None:
        # The first save happened on create; clear and update.
        service._storage.save.reset_mock()
        service.update_reminder(reminder.reminder_id, {"text": "edited"})
        service._storage.save.assert_called_once()
