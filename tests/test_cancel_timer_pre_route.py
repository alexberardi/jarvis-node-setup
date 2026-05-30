"""Unit tests for CancelTimerCommand pre-route."""

import pytest

from commands.cancel_timer_command import CancelTimerCommand


@pytest.fixture
def cmd():
    return CancelTimerCommand()


class TestPreRouteAll:
    @pytest.mark.parametrize("phrase", [
        "cancel all timers",
        "stop all my timers",
        "clear all timers",
        "delete all my timers",
        "remove all timers",
    ])
    def test_cancel_all(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"label": "all"}


class TestPreRouteNoLabel:
    @pytest.mark.parametrize("phrase", [
        "cancel my timer",
        "stop the timer",
        "cancel timer",
        "never mind the timer",
    ])
    def test_no_label(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {}


class TestPreRouteLabeled:
    @pytest.mark.parametrize("phrase,label", [
        ("cancel the pasta timer", "pasta"),
        ("stop the egg timer", "egg"),
        ("remove the nap timer", "nap"),
        ("delete the cooking timer", "cooking"),
        ("cancel my laundry timer", "laundry"),
    ])
    def test_labeled(self, cmd, phrase, label):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"label": label}


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "cancel",
        "tell me a joke",
        "set a 5 minute timer",
        "what time is it",
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_ids_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {
            "cancel_timer.all",
            "cancel_timer.none",
            "cancel_timer.labeled",
        }
