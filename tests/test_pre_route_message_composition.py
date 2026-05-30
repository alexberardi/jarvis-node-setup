"""Pre-route messages must be pre-composed.

The wrapper in `command_execution_service.try_pre_route` falls through to
the full LLM path if a pre-routed command's run() returns no spoken
`message`. That defeats the whole point of the fast path. This test
verifies every pre-routable shape returns a non-empty `context_data["message"]`
when called with `is_pre_routed=True`.

Regression guard for the bug where calculate/answer_question/etc. were
pre-routing but silently falling through because they only set
structured fields without composing a spoken response.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.request_information import RequestInformation


@pytest.fixture
def req():
    return RequestInformation(
        voice_command="",
        conversation_id="c",
        is_validation_response=False,
        is_pre_routed=True,
    )


class TestCalculator:
    def test_addition_message(self, req):
        from commands.calculator_command import CalculatorCommand
        cmd = CalculatorCommand()
        resp = cmd.run(req, num1=5, num2=3, operation="add")
        msg = resp.context_data.get("message")
        assert msg, "calculator must produce a spoken message on pre-route path"
        assert "5" in msg and "3" in msg and "8" in msg

    def test_division_message_decimal(self, req):
        from commands.calculator_command import CalculatorCommand
        cmd = CalculatorCommand()
        resp = cmd.run(req, num1=10, num2=4, operation="divide")
        msg = resp.context_data.get("message")
        assert msg
        assert "2.50" in msg or "2.5" in msg


class TestCheckTimers:
    def test_no_timers_message(self, req):
        with patch("services.timer_service.get_timer_service") as gts:
            ts = MagicMock()
            ts.get_active_timers.return_value = []
            gts.return_value = ts
            from commands.check_timers_command import CheckTimersCommand
            cmd = CheckTimersCommand()
            resp = cmd.run(req)
            assert resp.context_data.get("message")


class TestCancelTimer:
    def test_cancel_all_message(self, req):
        with patch("services.timer_service.get_timer_service") as gts:
            ts = MagicMock()
            ts.get_active_timers.return_value = [{"timer_id": "t1", "label": None}]
            ts.clear_all.return_value = 1
            gts.return_value = ts
            from commands.cancel_timer_command import CancelTimerCommand
            cmd = CancelTimerCommand()
            resp = cmd.run(req, label="all")
            assert resp.context_data.get("message")


class TestMeasurementConversion:
    def test_temperature_message(self, req):
        from commands.measurement_conversion_command import MeasurementConversionCommand
        cmd = MeasurementConversionCommand()
        resp = cmd.run(req, value=350, from_unit="fahrenheit", to_unit="celsius")
        msg = resp.context_data.get("message")
        assert msg
        assert "fahrenheit" in msg.lower() or "f" in msg.lower()

    def test_distance_message(self, req):
        from commands.measurement_conversion_command import MeasurementConversionCommand
        cmd = MeasurementConversionCommand()
        resp = cmd.run(req, value=5, from_unit="miles", to_unit="kilometers")
        msg = resp.context_data.get("message")
        assert msg
        assert "mile" in msg.lower() or "kilometer" in msg.lower()


class TestReminder:
    def test_list_empty_message(self, req):
        with patch("services.reminder_service.get_reminder_service") as grs:
            rs = MagicMock()
            rs.get_all_reminders.return_value = []
            grs.return_value = rs
            from commands.reminder_command import ReminderCommand
            cmd = ReminderCommand()
            resp = cmd.run(req, action="list")
            assert resp.context_data.get("message")


class TestTellJoke:
    def test_joke_message(self, req):
        from commands.tell_a_joke_command import TellAJokeCommand
        cmd = TellAJokeCommand()
        resp = cmd.run(req)
        msg = resp.context_data.get("message")
        assert msg
        assert len(msg) > 10


class TestAnswerQuestionHasNoPreRoute:
    """answer_question can't pre-compose a knowledge answer, so it must
    not declare any fast_path_patterns. Adding one would silently
    fall through to the LLM and waste a run() call."""

    def test_no_fast_path_patterns(self):
        from commands.answer_question_command import AnswerQuestionCommand
        cmd = AnswerQuestionCommand()
        assert cmd.fast_path_patterns == []

    def test_pre_route_returns_none(self):
        from commands.answer_question_command import AnswerQuestionCommand
        cmd = AnswerQuestionCommand()
        assert cmd.pre_route("tell me about Einstein") is None
        assert cmd.pre_route("explain photosynthesis") is None
