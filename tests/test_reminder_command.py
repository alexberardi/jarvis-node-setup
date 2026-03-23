"""Tests for ReminderCommand — set, list, delete, snooze actions."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from commands.reminder_command import ReminderCommand
from services.reminder_service import ReminderData, ReminderService


@pytest.fixture
def command() -> ReminderCommand:
    return ReminderCommand()


@pytest.fixture
def mock_service() -> MagicMock:
    return MagicMock(spec=ReminderService)


def _make_reminder(**overrides) -> ReminderData:
    defaults = {
        "reminder_id": "rem_test1234",
        "text": "call mom",
        "due_at": "2026-03-24T15:00:00+00:00",
        "created_at": "2026-03-23T10:00:00+00:00",
        "recurrence": None,
        "announced": False,
        "snooze_until": None,
        "announce_count": 0,
        "last_announced_at": None,
    }
    defaults.update(overrides)
    return ReminderData(**defaults)


class TestProperties:
    def test_command_name(self, command: ReminderCommand) -> None:
        assert command.command_name == "reminder"

    def test_description_mentions_remind(self, command: ReminderCommand) -> None:
        assert "remind" in command.description.lower()

    def test_keywords_include_remind(self, command: ReminderCommand) -> None:
        assert "remind" in command.keywords

    def test_has_action_parameter(self, command: ReminderCommand) -> None:
        names = [p.name for p in command.parameters]
        assert "action" in names

    def test_has_text_parameter(self, command: ReminderCommand) -> None:
        names = [p.name for p in command.parameters]
        assert "text" in names

    def test_no_required_secrets(self, command: ReminderCommand) -> None:
        assert command.required_secrets == []

    def test_antipatterns_exclude_timers(self, command: ReminderCommand) -> None:
        assert len(command.antipatterns) > 0
        for ap in command.antipatterns:
            assert "timer" in ap.example.lower() or "countdown" in ap.example.lower()


class TestPostProcess:
    def test_default_set_with_text(self, command: ReminderCommand) -> None:
        args = command.post_process_tool_call({"text": "call mom"}, "remind me to call mom")
        assert args["action"] == "set"

    def test_default_list_without_text(self, command: ReminderCommand) -> None:
        args = command.post_process_tool_call({}, "what reminders do I have")
        assert args["action"] == "list"

    def test_preserves_existing_action(self, command: ReminderCommand) -> None:
        args = command.post_process_tool_call({"action": "delete", "text": "mom"}, "cancel reminder")
        assert args["action"] == "delete"


class TestSetAction:
    @patch("commands.reminder_command.get_reminder_service")
    def test_set_with_relative_minutes(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        reminder = _make_reminder()
        svc.create_reminder.return_value = reminder
        mock_get_svc.return_value = svc

        result = command.run(None, action="set", text="call mom", relative_minutes=30)
        assert result.success
        assert "call mom" in result.context_data["text"]
        svc.create_reminder.assert_called_once()

    @patch("commands.reminder_command.get_reminder_service")
    def test_set_missing_text(self, mock_get_svc, command: ReminderCommand) -> None:
        mock_get_svc.return_value = MagicMock()
        result = command.run(None, action="set", relative_minutes=30)
        assert not result.success
        assert "what" in result.error_details.lower()

    @patch("commands.reminder_command.get_reminder_service")
    def test_set_missing_time(self, mock_get_svc, command: ReminderCommand) -> None:
        mock_get_svc.return_value = MagicMock()
        result = command.run(None, action="set", text="test")
        assert not result.success
        assert "when" in result.error_details.lower()

    @patch("commands.reminder_command.get_reminder_service")
    def test_set_with_date_keys_and_time(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.create_reminder.return_value = _make_reminder()
        mock_get_svc.return_value = svc

        result = command.run(
            None, action="set", text="meeting",
            resolved_datetimes=["tomorrow"], time="15:00",
        )
        assert result.success

    @patch("commands.reminder_command.get_reminder_service")
    def test_set_with_recurrence(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.create_reminder.return_value = _make_reminder(recurrence="daily")
        mock_get_svc.return_value = svc

        result = command.run(
            None, action="set", text="medicine",
            time="08:00", recurrence="daily",
        )
        assert result.success
        assert result.context_data["recurrence"] == "daily"


class TestListAction:
    @patch("commands.reminder_command.get_reminder_service")
    def test_list_all(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.get_all_reminders.return_value = [
            _make_reminder(text="a"),
            _make_reminder(text="b", reminder_id="rem_22222222"),
        ]
        mock_get_svc.return_value = svc

        result = command.run(None, action="list")
        assert result.success
        assert result.context_data["count"] == 2

    @patch("commands.reminder_command.get_reminder_service")
    def test_list_empty(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.get_all_reminders.return_value = []
        mock_get_svc.return_value = svc

        result = command.run(None, action="list")
        assert result.success
        assert result.context_data["count"] == 0


class TestDeleteAction:
    @patch("commands.reminder_command.get_reminder_service")
    def test_delete_by_text(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.find_by_text.return_value = _make_reminder()
        svc.delete_reminder.return_value = True
        mock_get_svc.return_value = svc

        result = command.run(None, action="delete", text="call mom")
        assert result.success
        svc.delete_reminder.assert_called_once()

    @patch("commands.reminder_command.get_reminder_service")
    def test_delete_not_found(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.find_by_text.return_value = None
        mock_get_svc.return_value = svc

        result = command.run(None, action="delete", text="nonexistent")
        assert not result.success

    @patch("commands.reminder_command.get_reminder_service")
    def test_delete_all(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.delete_all_reminders.return_value = 3
        mock_get_svc.return_value = svc

        result = command.run(None, action="delete", scope="all")
        assert result.success
        assert result.context_data["deleted_count"] == 3

    @patch("commands.reminder_command.get_reminder_service")
    def test_delete_missing_text(self, mock_get_svc, command: ReminderCommand) -> None:
        mock_get_svc.return_value = MagicMock()
        result = command.run(None, action="delete")
        assert not result.success


class TestSnoozeAction:
    @patch("commands.reminder_command.get_reminder_service")
    def test_snooze_recent(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        snoozed = _make_reminder(
            snooze_until=(datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        )
        svc.find_most_recently_announced.return_value = _make_reminder()
        svc.snooze_reminder.return_value = snoozed
        mock_get_svc.return_value = svc

        result = command.run(None, action="snooze")
        assert result.success
        svc.snooze_reminder.assert_called_once()

    @patch("commands.reminder_command.get_reminder_service")
    def test_snooze_by_text(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        snoozed = _make_reminder(
            snooze_until=(datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat(),
        )
        svc.find_by_text.return_value = _make_reminder()
        svc.snooze_reminder.return_value = snoozed
        mock_get_svc.return_value = svc

        result = command.run(None, action="snooze", text="mom", minutes=15)
        assert result.success
        svc.find_by_text.assert_called_with("mom")

    @patch("commands.reminder_command.get_reminder_service")
    def test_snooze_no_recent(self, mock_get_svc, command: ReminderCommand) -> None:
        svc = MagicMock()
        svc.find_most_recently_announced.return_value = None
        mock_get_svc.return_value = svc

        result = command.run(None, action="snooze")
        assert not result.success


class TestExamples:
    def test_prompt_examples_have_primary(self, command: ReminderCommand) -> None:
        examples = command.generate_prompt_examples()
        primaries = [e for e in examples if e.is_primary]
        assert len(primaries) == 1

    def test_adapter_examples_cover_all_actions(self, command: ReminderCommand) -> None:
        examples = command.generate_adapter_examples()
        actions = {e.expected_parameters.get("action") for e in examples}
        assert {"set", "list", "delete", "snooze"} <= actions

    def test_adapter_examples_count(self, command: ReminderCommand) -> None:
        examples = command.generate_adapter_examples()
        assert len(examples) >= 20
