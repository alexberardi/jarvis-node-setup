"""reminder command's `set_at` proposable action — the "leave by" card's confirm path.

Uses an ABSOLUTE due_at (not relative_minutes) because a proposal card is tapped
later than it's proposed; a relative offset would drift by however long it waited.
"""
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from commands.reminder_command import ReminderCommand
from core.request_information import RequestInformation

_UID = 42


def _req(user_id=_UID) -> RequestInformation:
    return RequestInformation(voice_command="", conversation_id="c", user_id=user_id)


def _reminder():
    r = MagicMock()
    r.reminder_id = "rem_1"
    r.due_at = "2026-08-13T14:40:00+00:00"
    return r


def test_proposable_action_declared():
    actions = ReminderCommand().proposable_actions
    assert len(actions) == 1
    action = actions[0]
    assert action.callback == "set_at"
    assert action.idempotency_param == "idempotency_key"
    names = {p.name for p in action.params}
    assert {"text", "due_at_iso", "idempotency_key"} <= names


@patch("commands.reminder_command.get_reminder_service")
def test_set_at_creates_reminder_at_absolute_time(mock_get_svc):
    svc = MagicMock()
    svc.create_reminder.return_value = _reminder()
    mock_get_svc.return_value = svc
    with patch("commands.reminder_command.ReminderService.format_due_at_human", return_value="2:40 PM"):
        ReminderCommand().set_at(
            {"text": "Leave for Dentist", "due_at_iso": "2026-08-13T14:40:00+00:00",
             "idempotency_key": "k1"},
            _req())
    args, kwargs = svc.create_reminder.call_args
    assert args[0] == "Leave for Dentist"
    assert args[1] == datetime(2026, 8, 13, 14, 40, tzinfo=timezone.utc)   # parsed absolute instant
    assert kwargs["user_id"] == _UID


@patch("commands.reminder_command.get_reminder_service")
def test_set_at_refuses_unknown_speaker(mock_get_svc):
    svc = MagicMock()
    mock_get_svc.return_value = svc
    ReminderCommand().set_at(
        {"text": "x", "due_at_iso": "2026-08-13T14:40:00+00:00", "idempotency_key": "k"},
        _req(user_id=None))
    svc.create_reminder.assert_not_called()


@patch("commands.reminder_command.get_reminder_service")
def test_set_at_rejects_invalid_due_at(mock_get_svc):
    svc = MagicMock()
    mock_get_svc.return_value = svc
    ReminderCommand().set_at({"text": "x", "due_at_iso": "not-a-date", "idempotency_key": "k"}, _req())
    svc.create_reminder.assert_not_called()
