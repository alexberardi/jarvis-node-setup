"""Unit tests for CalculatorCommand pre-route.

Covers the deterministic arithmetic regex parser that bypasses the LLM.
"""

import pytest

from commands.calculator_command import CalculatorCommand


@pytest.fixture
def cmd():
    return CalculatorCommand()


class TestPreRouteInfix:
    @pytest.mark.parametrize("phrase,expected", [
        ("5 plus 3", {"num1": 5.0, "num2": 3.0, "operation": "add"}),
        ("what's 5 plus 3", {"num1": 5.0, "num2": 3.0, "operation": "add"}),
        ("what is 5 plus 3?", {"num1": 5.0, "num2": 3.0, "operation": "add"}),
        ("10 minus 4", {"num1": 10.0, "num2": 4.0, "operation": "subtract"}),
        ("6 times 7", {"num1": 6.0, "num2": 7.0, "operation": "multiply"}),
        ("what's 6 times 7?", {"num1": 6.0, "num2": 7.0, "operation": "multiply"}),
        ("20 divided by 5", {"num1": 20.0, "num2": 5.0, "operation": "divide"}),
        ("3.5 plus 2.1", {"num1": 3.5, "num2": 2.1, "operation": "add"}),
        ("100 over 4", {"num1": 100.0, "num2": 4.0, "operation": "divide"}),
        ("9 multiplied by 8", {"num1": 9.0, "num2": 8.0, "operation": "multiply"}),
    ])
    def test_infix(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None, f"No match for {phrase!r}"
        assert result.arguments == expected


class TestPreRouteImperative:
    @pytest.mark.parametrize("phrase,expected", [
        ("add 5 and 3", {"num1": 5.0, "num2": 3.0, "operation": "add"}),
        ("multiply 6 by 7", {"num1": 6.0, "num2": 7.0, "operation": "multiply"}),
        ("divide 20 by 5", {"num1": 20.0, "num2": 5.0, "operation": "divide"}),
        # "subtract 7 from 22" means 22 - 7
        ("subtract 7 from 22", {"num1": 22.0, "num2": 7.0, "operation": "subtract"}),
    ])
    def test_imperative(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None, f"No match for {phrase!r}"
        assert result.arguments == expected


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "tell me a joke",
        "set a timer",
        "what is the weather",
        "what time is it",
        "what is 5",          # only one number
        "5 plus",             # missing operand
        "five plus three",    # spelled-out numbers not supported in pre-route
        "what's the square root of 144",
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestPreRouteDisabledIds:
    def test_disabling_skips_match(self, cmd):
        # Same shape that normally matches via "calculate.infix"
        result = cmd.pre_route("5 plus 3", disabled_pattern_ids={"calculate.infix"})
        # Imperative would still take "add 5 and 3" but not infix shape
        assert result is None

    def test_disabling_one_doesnt_break_other(self, cmd):
        result = cmd.pre_route("add 5 and 3", disabled_pattern_ids={"calculate.infix"})
        assert result is not None
        assert result.arguments == {"num1": 5.0, "num2": 3.0, "operation": "add"}


class TestFastPathPatterns:
    def test_patterns_declared(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert {"calculate.infix", "calculate.imperative"} <= ids

    def test_pattern_ids_stable(self, cmd):
        # Ids must be stable across releases so user-disabled patterns
        # survive package upgrades. Pin them here.
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {"calculate.infix", "calculate.imperative"}
