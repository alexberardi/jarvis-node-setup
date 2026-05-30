"""Unit tests for CheckTimersCommand pre-route."""

import pytest

from commands.check_timers_command import CheckTimersCommand


@pytest.fixture
def cmd():
    return CheckTimersCommand()


class TestPreRouteGlobal:
    @pytest.mark.parametrize("phrase", [
        "how much time is left",
        "how much time is left?",
        "how long is left",
        "how long do I have",
        "time left",
        "time remaining",
        "what timers are running",
        "what timer do I have",
        "check my timers",
        "check timer",
        "timer status",
    ])
    def test_global_no_label(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {}


class TestPreRouteLabeled:
    @pytest.mark.parametrize("phrase,label", [
        ("check the pasta timer", "pasta"),
        ("how long until the egg timer", "egg"),
        ("what's the status of the laundry timer", "laundry"),
        ("how much on the nap timer", "nap"),
        ("time left on my tea timer", "tea"),
        ("is the cooking timer still running", "cooking"),
    ])
    def test_labeled(self, cmd, phrase, label):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"label": label}


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "set a 5 minute timer",
        "cancel my timer",
        "tell me a joke",
        "what time is it",
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_ids_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {"check_timers.labeled", "check_timers.global"}
