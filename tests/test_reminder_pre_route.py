"""Unit tests for ReminderCommand pre-route.

Only the deterministic actions (list, delete-all, snooze) are pre-routed.
Set/delete-by-label rely on LLM date and fuzzy text parsing.
"""

import pytest

from commands.reminder_command import ReminderCommand


@pytest.fixture
def cmd():
    return ReminderCommand()


class TestPreRouteList:
    @pytest.mark.parametrize("phrase", [
        "what reminders do I have",
        "list my reminders",
        "show me my reminders",
        "show my reminders",
        "do I have any upcoming reminders",
        "any reminders",
    ])
    def test_list_all(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "list"}

    @pytest.mark.parametrize("phrase", [
        "any reminders for today",
        "reminders for today",
        "reminders today",
        "do I have any reminders today",
    ])
    def test_list_today(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "list", "filter": "today"}

    @pytest.mark.parametrize("phrase", [
        "show my recurring reminders",
        "show me my recurring reminders",
        "recurring reminders",
        "list my recurring reminders",
    ])
    def test_list_recurring(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "list", "filter": "recurring"}


class TestPreRouteDeleteAll:
    @pytest.mark.parametrize("phrase", [
        "delete all my reminders",
        "cancel all my reminders",
        "clear all reminders",
        "remove all my reminders",
    ])
    def test_delete_all(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "delete", "scope": "all"}


class TestPreRouteSnooze:
    @pytest.mark.parametrize("phrase", [
        "snooze",
        "snooze that",
        "snooze it",
        "snooze the reminder",
    ])
    def test_snooze_bare(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "snooze"}

    @pytest.mark.parametrize("phrase,expected_min", [
        ("snooze for 15 minutes", 15),
        ("snooze for 30 mins", 30),
        ("snooze for 5 min", 5),
        ("snooze for 1 hour", 60),
        ("snooze for 2 hours", 120),
        ("snooze that for 10 minutes", 10),
    ])
    def test_snooze_for(self, cmd, phrase, expected_min):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "snooze", "minutes": expected_min}


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "remind me to call mom at 3 PM",         # set with date
        "remind me in 30 minutes to take out trash",
        "remind me every day at 8 AM to exercise",
        "cancel my reminder to call mom",         # delete with label
        "snooze the medicine reminder for 15 minutes",  # snooze with label
        "tell me a joke",
        "what time is it",
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_ids_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {
            "reminder.list",
            "reminder.list_today",
            "reminder.list_recurring",
            "reminder.delete_all",
            "reminder.snooze",
            "reminder.snooze_for",
        }
