"""Tests for ReminderService — CRUD, recurrence, snooze, date resolution."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from services.reminder_service import (
    ReminderData,
    ReminderService,
    has_explicit_time,
)


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
        # Compare in local time — "tomorrow at 3 PM" means 3 PM on the user's clock
        local_due = due.astimezone()
        expected_date = (datetime.now().astimezone() + timedelta(days=1)).date()
        assert local_due.date() == expected_date
        assert local_due.hour == 15
        assert local_due.minute == 0

    def test_date_key_today_with_time(self) -> None:
        due = ReminderService.resolve_due_at(["today"], "23:59")
        assert due is not None
        assert due.astimezone().date() == datetime.now().astimezone().date()

    def test_date_key_morning_default_hour(self) -> None:
        due = ReminderService.resolve_due_at(["morning"])
        assert due is not None
        assert due.astimezone().hour == 7

    def test_date_key_tomorrow_evening(self) -> None:
        due = ReminderService.resolve_due_at(["tomorrow_evening"])
        assert due is not None
        local_due = due.astimezone()
        expected_date = (datetime.now().astimezone() + timedelta(days=1)).date()
        assert local_due.date() == expected_date
        assert local_due.hour == 19

    def test_time_only_future_today(self) -> None:
        # Use 23:59 to ensure it's in the future
        due = ReminderService.resolve_due_at(time_str="23:59")
        assert due is not None
        now = datetime.now().astimezone()
        if now.hour < 23 or (now.hour == 23 and now.minute < 59):
            assert due.astimezone().date() == now.date()

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
        local_due = due.astimezone()
        assert local_due.weekday() == 0  # Monday
        assert local_due.hour == 9

    def test_returned_datetime_is_timezone_aware(self) -> None:
        due = ReminderService.resolve_due_at(["tomorrow"], "10:00")
        assert due is not None
        assert due.tzinfo is not None

    def test_time_reflects_local_wall_clock(self) -> None:
        """Regression: "10 AM tomorrow" must mean 10 AM local, not 10 AM UTC.

        Before the fix this returned a datetime stamped with tzinfo=UTC but
        carrying local-time components, so 10 AM in EST would have fired at
        5 AM local (10 UTC = 5 EST).
        """
        due = ReminderService.resolve_due_at(["tomorrow"], "10:00")
        assert due is not None
        local_due = due.astimezone()
        assert local_due.hour == 10
        assert local_due.minute == 0


class TestHasExplicitTime:
    def test_explicit_time(self) -> None:
        assert has_explicit_time(None, "15:00", None) is True

    def test_relative_minutes(self) -> None:
        assert has_explicit_time(None, None, 30) is True

    def test_time_of_day_key(self) -> None:
        assert has_explicit_time(["tomorrow_morning"], None, None) is True

    def test_bare_tomorrow_no_time(self) -> None:
        assert has_explicit_time(["tomorrow"], None, None) is False

    def test_weekday_no_time(self) -> None:
        assert has_explicit_time(["next_monday"], None, None) is False

    def test_no_inputs(self) -> None:
        assert has_explicit_time(None, None, None) is False

    def test_zero_relative_minutes(self) -> None:
        # 0 minutes "remind me in 0 minutes" isn't really a time
        assert has_explicit_time(None, None, 0) is False


class TestBiweeklyRecurrence:
    def test_biweekly_advances_two_weeks(self, service: ReminderService) -> None:
        due = datetime(2026, 6, 7, 12, 0, tzinfo=timezone.utc)  # Sunday
        reminder = service.create_reminder("call grandma", due, recurrence="biweekly")
        service.mark_announced(reminder.reminder_id)
        updated = service.get_reminder(reminder.reminder_id)
        new_due = datetime.fromisoformat(updated.due_at)
        assert new_due == due + timedelta(weeks=2)
        # And it remains a Sunday
        assert new_due.weekday() == 6


class TestUserScoping:
    def test_create_with_user_id(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        reminder = service.create_reminder("alex thing", due, user_id=42)
        assert reminder.user_id == 42

    def test_get_all_filters_by_user(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("alex thing", due, user_id=42)
        service.create_reminder("bob thing", due, user_id=99)
        alex = service.get_all_reminders(user_id=42)
        bob = service.get_all_reminders(user_id=99)
        assert [r.text for r in alex] == ["alex thing"]
        assert [r.text for r in bob] == ["bob thing"]

    def test_get_all_includes_legacy_unscoped(self, service: ReminderService) -> None:
        """Reminders saved before user scoping (user_id=None) are visible to everyone."""
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("legacy", due, user_id=None)
        service.create_reminder("alex thing", due, user_id=42)
        alex = service.get_all_reminders(user_id=42)
        assert {r.text for r in alex} == {"alex thing", "legacy"}

    def test_find_by_text_scoped(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("call mom", due, user_id=42)
        service.create_reminder("call mom", due, user_id=99)
        found = service.find_by_text("mom", user_id=42)
        assert found is not None
        assert found.user_id == 42

    def test_find_by_text_does_not_leak_across_users(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("private alex thing", due, user_id=42)
        # bob looking for alex's reminder shouldn't find it
        found = service.find_by_text("private", user_id=99)
        assert found is None

    def test_delete_all_only_removes_caller_reminders(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        service.create_reminder("alex a", due, user_id=42)
        service.create_reminder("alex b", due, user_id=42)
        service.create_reminder("bob a", due, user_id=99)
        count = service.delete_all_reminders(user_id=42)
        assert count == 2
        # Bob's reminder survives
        assert {r.text for r in service.get_all_reminders(user_id=99)} == {"bob a"}

    def test_find_most_recently_announced_scoped(self, service: ReminderService) -> None:
        past = datetime.now(timezone.utc) - timedelta(minutes=1)
        bob = service.create_reminder("bob's thing", past, user_id=99)
        service.mark_announced(bob.reminder_id)
        # Alex shouldn't see Bob's most-recent announcement
        assert service.find_most_recently_announced(user_id=42) is None
        assert service.find_most_recently_announced(user_id=99) is not None

    def test_get_reminder_refuses_cross_user(self, service: ReminderService) -> None:
        due = datetime.now(timezone.utc) + timedelta(hours=1)
        bob = service.create_reminder("bob private", due, user_id=99)
        assert service.get_reminder(bob.reminder_id, user_id=42) is None
        assert service.get_reminder(bob.reminder_id, user_id=99) is not None


class TestReminderData:
    def test_round_trip(self) -> None:
        data = ReminderData(
            reminder_id="rem_test1234",
            text="call mom",
            due_at="2026-03-24T15:00:00+00:00",
            created_at="2026-03-23T10:00:00+00:00",
            recurrence="daily",
            user_id=42,
        )
        d = data.to_dict()
        restored = ReminderData.from_dict(d)
        assert restored.reminder_id == data.reminder_id
        assert restored.text == data.text
        assert restored.recurrence == data.recurrence
        assert restored.user_id == 42

    def test_round_trip_no_user_id(self) -> None:
        """Legacy records (pre-user-scoping) lack user_id — restoration defaults to None."""
        data = {
            "reminder_id": "rem_legacy",
            "text": "old",
            "due_at": "2026-03-24T15:00:00+00:00",
            "created_at": "2026-03-23T10:00:00+00:00",
        }
        restored = ReminderData.from_dict(data)
        assert restored.user_id is None

    def test_is_recurring(self) -> None:
        data = ReminderData(
            reminder_id="rem_1", text="test",
            due_at="2026-03-24T15:00:00+00:00",
            created_at="2026-03-23T10:00:00+00:00",
        )
        assert data.is_recurring is False
        data.recurrence = "daily"
        assert data.is_recurring is True
