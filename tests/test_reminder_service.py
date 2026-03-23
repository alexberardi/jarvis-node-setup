"""Tests for ReminderService — CRUD, recurrence, snooze, date resolution."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.reminder_service import ReminderData, ReminderService


@pytest.fixture
def service() -> ReminderService:
    svc = ReminderService()
    svc._storage = MagicMock()
    return svc


class TestCreateReminder:
    def test_basic(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 15, 0, tzinfo=timezone.utc)
        reminder = service.create_reminder("call mom", due)
        assert reminder.text == "call mom"
        assert reminder.reminder_id.startswith("rem_")
        assert reminder.announced is False
        assert reminder.recurrence is None
        service._storage.save.assert_called_once()

    def test_with_recurrence(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 8, 0, tzinfo=timezone.utc)
        reminder = service.create_reminder("take medicine", due, recurrence="daily")
        assert reminder.recurrence == "daily"
        assert reminder.is_recurring is True

    def test_naive_datetime_gets_utc(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 15, 0)  # naive
        reminder = service.create_reminder("test", due)
        parsed = datetime.fromisoformat(reminder.due_at)
        assert parsed.tzinfo is not None


class TestGetReminder:
    def test_by_id(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 15, 0, tzinfo=timezone.utc)
        reminder = service.create_reminder("test", due)
        found = service.get_reminder(reminder.reminder_id)
        assert found is not None
        assert found.text == "test"

    def test_not_found(self, service: ReminderService) -> None:
        assert service.get_reminder("rem_nonexistent") is None


class TestGetAllReminders:
    def test_returns_all(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 15, 0, tzinfo=timezone.utc)
        service.create_reminder("a", due)
        service.create_reminder("b", due + timedelta(hours=1))
        service.create_reminder("c", due + timedelta(hours=2))
        assert len(service.get_all_reminders()) == 3

    def test_excludes_announced_by_default(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 15, 0, tzinfo=timezone.utc)
        r1 = service.create_reminder("a", due)
        service.create_reminder("b", due)
        service.mark_announced(r1.reminder_id)
        assert len(service.get_all_reminders()) == 1
        assert len(service.get_all_reminders(include_announced=True)) == 2

    def test_sorted_by_due_at(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 15, 0, tzinfo=timezone.utc)
        service.create_reminder("later", due + timedelta(hours=2))
        service.create_reminder("sooner", due)
        reminders = service.get_all_reminders()
        assert reminders[0].text == "sooner"
        assert reminders[1].text == "later"


class TestGetDueReminders:
    def test_past_due_returned(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        service.create_reminder("overdue", past)
        assert len(service.get_due_reminders()) == 1

    def test_future_excluded(self, service: ReminderService) -> None:
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("not yet", future)
        assert len(service.get_due_reminders()) == 0

    def test_snoozed_excluded(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        reminder = service.create_reminder("snoozed", past)
        service.snooze_reminder(reminder.reminder_id, minutes=30)
        assert len(service.get_due_reminders()) == 0

    def test_announced_excluded(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=5)
        reminder = service.create_reminder("done", past)
        service.mark_announced(reminder.reminder_id)
        assert len(service.get_due_reminders()) == 0


class TestMarkAnnounced:
    def test_one_shot(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        reminder = service.create_reminder("test", past)
        service.mark_announced(reminder.reminder_id)
        updated = service.get_reminder(reminder.reminder_id)
        assert updated.announced is True
        assert updated.announce_count == 1
        assert updated.last_announced_at is not None

    def test_recurring_daily_advances(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 8, 0, tzinfo=timezone.utc)
        reminder = service.create_reminder("medicine", due, recurrence="daily")
        service.mark_announced(reminder.reminder_id)
        updated = service.get_reminder(reminder.reminder_id)
        assert updated.announced is False  # Reset for next occurrence
        new_due = datetime.fromisoformat(updated.due_at)
        assert new_due == due + timedelta(days=1)

    def test_recurring_weekly_advances(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 24, 9, 0, tzinfo=timezone.utc)  # Monday
        reminder = service.create_reminder("timesheet", due, recurrence="weekly")
        service.mark_announced(reminder.reminder_id)
        updated = service.get_reminder(reminder.reminder_id)
        new_due = datetime.fromisoformat(updated.due_at)
        assert new_due == due + timedelta(weeks=1)

    def test_recurring_weekdays_skips_weekend(self, service: ReminderService) -> None:
        # Friday March 27, 2026
        due = datetime(2026, 3, 27, 8, 0, tzinfo=timezone.utc)
        assert due.weekday() == 4  # Friday
        reminder = service.create_reminder("standup", due, recurrence="weekdays")
        service.mark_announced(reminder.reminder_id)
        updated = service.get_reminder(reminder.reminder_id)
        new_due = datetime.fromisoformat(updated.due_at)
        assert new_due.weekday() == 0  # Monday
        assert new_due == datetime(2026, 3, 30, 8, 0, tzinfo=timezone.utc)

    def test_recurring_monthly_advances(self, service: ReminderService) -> None:
        due = datetime(2026, 3, 15, 9, 0, tzinfo=timezone.utc)
        reminder = service.create_reminder("pay bills", due, recurrence="monthly")
        service.mark_announced(reminder.reminder_id)
        updated = service.get_reminder(reminder.reminder_id)
        new_due = datetime.fromisoformat(updated.due_at)
        assert new_due.month == 4
        assert new_due.day == 15


class TestSnooze:
    def test_default_10_minutes(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        reminder = service.create_reminder("test", past)
        service.mark_announced(reminder.reminder_id)
        snoozed = service.snooze_reminder(reminder.reminder_id)
        assert snoozed is not None
        assert snoozed.announced is False
        assert snoozed.snooze_until is not None

    def test_custom_duration(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        reminder = service.create_reminder("test", past)
        snoozed = service.snooze_reminder(reminder.reminder_id, minutes=30)
        snooze_dt = datetime.fromisoformat(snoozed.snooze_until)
        # Should be roughly 30 minutes from now
        expected = datetime.now(timezone.utc) + timedelta(minutes=30)
        assert abs((snooze_dt - expected).total_seconds()) < 5

    def test_nonexistent_returns_none(self, service: ReminderService) -> None:
        assert service.snooze_reminder("rem_nope") is None


class TestDelete:
    def test_delete_one(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create_reminder("test", due)
        assert service.delete_reminder(reminder.reminder_id) is True
        assert service.get_reminder(reminder.reminder_id) is None
        service._storage.delete.assert_called_with(reminder.reminder_id)

    def test_delete_nonexistent(self, service: ReminderService) -> None:
        assert service.delete_reminder("rem_nope") is False

    def test_delete_all(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("a", due)
        service.create_reminder("b", due)
        count = service.delete_all_reminders()
        assert count == 2
        assert len(service.get_all_reminders()) == 0


class TestFindByText:
    def test_exact_match(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("call mom", due)
        found = service.find_by_text("call mom")
        assert found is not None
        assert found.text == "call mom"

    def test_partial_match(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("call mom about dinner", due)
        found = service.find_by_text("mom")
        assert found is not None

    def test_case_insensitive(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("Call Mom", due)
        found = service.find_by_text("call mom")
        assert found is not None

    def test_not_found(self, service: ReminderService) -> None:
        assert service.find_by_text("nonexistent") is None


class TestFindMostRecentlyAnnounced:
    def test_finds_recent(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        reminder = service.create_reminder("test", past)
        service.mark_announced(reminder.reminder_id)
        found = service.find_most_recently_announced()
        assert found is not None
        assert found.reminder_id == reminder.reminder_id

    def test_outside_window(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        reminder = service.create_reminder("test", past)
        service.mark_announced(reminder.reminder_id)
        # Set last_announced_at to 10 minutes ago (outside 5-min window)
        reminder.last_announced_at = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        found = service.find_most_recently_announced()
        assert found is None


class TestDateResolution:
    def test_date_key_tomorrow_with_time(self) -> None:
        due = ReminderService.resolve_due_at(["tomorrow"], "15:00")
        assert due is not None
        expected_date = (datetime.now() + timedelta(days=1)).date()
        assert due.date() == expected_date
        assert due.hour == 15
        assert due.minute == 0

    def test_date_key_today_with_time(self) -> None:
        due = ReminderService.resolve_due_at(["today"], "23:59")
        assert due is not None
        assert due.date() == datetime.now().date()

    def test_date_key_morning_default_hour(self) -> None:
        due = ReminderService.resolve_due_at(["morning"])
        assert due is not None
        assert due.hour == 7

    def test_date_key_tomorrow_evening(self) -> None:
        due = ReminderService.resolve_due_at(["tomorrow_evening"])
        assert due is not None
        expected_date = (datetime.now() + timedelta(days=1)).date()
        assert due.date() == expected_date
        assert due.hour == 19

    def test_time_only_future_today(self) -> None:
        # Use 23:59 to ensure it's in the future
        due = ReminderService.resolve_due_at(time_str="23:59")
        assert due is not None
        now = datetime.now()
        if now.hour < 23 or (now.hour == 23 and now.minute < 59):
            assert due.date() == now.date()

    def test_relative_minutes(self) -> None:
        due = ReminderService.resolve_due_at(relative_minutes=30)
        assert due is not None
        expected = datetime.now(timezone.utc) + timedelta(minutes=30)
        assert abs((due - expected).total_seconds()) < 5

    def test_no_params_returns_none(self) -> None:
        assert ReminderService.resolve_due_at() is None

    def test_next_weekday_key(self) -> None:
        due = ReminderService.resolve_due_at(["next_monday"], "09:00")
        assert due is not None
        assert due.weekday() == 0  # Monday
        assert due.hour == 9


class TestReminderData:
    def test_round_trip(self) -> None:
        data = ReminderData(
            reminder_id="rem_test1234",
            text="call mom",
            due_at="2026-03-24T15:00:00+00:00",
            created_at="2026-03-23T10:00:00+00:00",
            recurrence="daily",
        )
        d = data.to_dict()
        restored = ReminderData.from_dict(d)
        assert restored.reminder_id == data.reminder_id
        assert restored.text == data.text
        assert restored.recurrence == data.recurrence

    def test_is_recurring(self) -> None:
        data = ReminderData(
            reminder_id="rem_1", text="test",
            due_at="2026-03-24T15:00:00+00:00",
            created_at="2026-03-23T10:00:00+00:00",
        )
        assert data.is_recurring is False
        data.recurrence = "daily"
        assert data.is_recurring is True
