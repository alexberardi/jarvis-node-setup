"""Unit tests for TimezoneCommand pre-route.

Regression guard for the bug where "what day is it?" routed to
get_current_time but spoke the *time* instead of the *date* — the original
local-time fast-path regex matched both time and date queries but the
message composition in run() only ever said the time.
"""

from datetime import datetime
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from commands.timezone_command import TimezoneCommand
from core.request_information import RequestInformation


@pytest.fixture
def cmd():
    return TimezoneCommand()


@pytest.fixture
def req():
    return RequestInformation(
        voice_command="",
        conversation_id="c",
        is_validation_response=False,
        is_pre_routed=True,
    )


class TestPreRouteRouting:
    """The fast-path regex must match each phrasing and route to the right handler."""

    @pytest.mark.parametrize("phrase", [
        "what time is it",
        "what time is it?",
        "what's the time",
        "what is the time",
        "current time",
        "the time",
    ])
    def test_local_time_phrasings_match(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None, f"No match for {phrase!r}"
        assert result.arguments == {}
        # Time pattern lets run() compose the message; no override.
        assert result.spoken_response is None

    @pytest.mark.parametrize("phrase", [
        "what day is it",
        "what day is it?",
        "what day is today",
        "what's the date",
        "what is the date",
        "what's date",
        "what's today's date",
        "what is today's date",
        "what day are we",
        "what day is we",
    ])
    def test_local_date_phrasings_match(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None, f"No match for {phrase!r}"
        assert result.arguments == {}
        # Date handler overrides spoken_response so the user hears the date,
        # not the time — this is the core fix.
        assert result.spoken_response, (
            f"date handler must override spoken_response for {phrase!r}"
        )

    @pytest.mark.parametrize("phrase,location", [
        ("what time is it in Tokyo", "Tokyo"),
        ("what time is it in tokyo?", "tokyo"),
        ("what's the time in London", "London"),
        ("current time in Berlin", "Berlin"),
        ("time in New York", "New York"),
    ])
    def test_time_in_location_matches(self, cmd, phrase, location):
        result = cmd.pre_route(phrase)
        assert result is not None, f"No match for {phrase!r}"
        assert result.arguments == {"location": location}


class TestDateHandlerMessage:
    """The date handler must compose a date-shaped message, not a time-shaped one."""

    def test_message_contains_day_of_week(self, cmd):
        fixed = datetime(2026, 1, 28, 15, 45, tzinfo=ZoneInfo("America/New_York"))
        with patch("commands.timezone_command.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = cmd.pre_route("what day is it")
        assert result is not None
        # 2026-01-28 is a Wednesday.
        assert "Wednesday" in result.spoken_response
        assert "January" in result.spoken_response
        assert "28" in result.spoken_response

    def test_message_does_not_leak_clock_time(self, cmd):
        fixed = datetime(2026, 1, 28, 15, 45, tzinfo=ZoneInfo("America/New_York"))
        with patch("commands.timezone_command.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = cmd.pre_route("what's the date")
        assert result is not None
        # Bug regression: the previous implementation said "It's 3:45 PM."
        # for date queries. Make sure we never include the clock time here.
        msg = result.spoken_response
        assert "PM" not in msg and "AM" not in msg
        assert "3:45" not in msg

    def test_unpadded_day_for_single_digit(self, cmd):
        # Speaking "January 8" is natural; "January 08" is awkward TTS.
        fixed = datetime(2026, 1, 8, 9, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("commands.timezone_command.datetime") as mock_dt:
            mock_dt.now.return_value = fixed
            result = cmd.pre_route("what day is it")
        assert result is not None
        assert "January 8" in result.spoken_response
        assert "January 08" not in result.spoken_response


class TestRunMessageComposition:
    """End-to-end run() with is_pre_routed=True must produce the right message."""

    def test_local_time_message_is_time_shaped(self, cmd, req):
        # The "what time is it" path: run() composes "It's H:MM AM/PM."
        resp = cmd.run(req)
        msg = resp.context_data.get("message")
        assert msg
        assert "It's" in msg
        # Time should include AM or PM.
        assert "AM" in msg or "PM" in msg

    def test_location_message_is_time_shaped(self, cmd, req):
        resp = cmd.run(req, location="Tokyo")
        msg = resp.context_data.get("message")
        assert msg
        assert "Tokyo" in msg
        assert "AM" in msg or "PM" in msg


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "tell me a joke",
        "set a timer",
        "what's the weather",
        # Pure date-in-location queries aren't claimed by the fast path —
        # they fall through to the LLM. Don't over-promise.
        "what day is it in Tokyo",
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_patterns_declared(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert {
            "get_current_time.in_location",
            "get_current_time.local",
            "get_current_time.local_date",
        } <= ids

    def test_pattern_ids_stable(self, cmd):
        # Ids must be stable across releases so user-disabled patterns
        # survive package upgrades. Pin them here.
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {
            "get_current_time.in_location",
            "get_current_time.local",
            "get_current_time.local_date",
        }


class TestDisabledIds:
    def test_disabling_date_pattern_falls_through(self, cmd):
        result = cmd.pre_route(
            "what day is it",
            disabled_pattern_ids={"get_current_time.local_date"},
        )
        # With the date pattern disabled, this phrase doesn't match anything.
        assert result is None

    def test_disabling_date_doesnt_break_time(self, cmd):
        result = cmd.pre_route(
            "what time is it",
            disabled_pattern_ids={"get_current_time.local_date"},
        )
        assert result is not None
        assert result.arguments == {}
