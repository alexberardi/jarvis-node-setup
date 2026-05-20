"""Unit tests for ControlNodeCommand.

Covers the pre_route regex parser (the hot path — bypasses the LLM)
and the run() dispatcher for each action. Audio backend calls are
mocked via monkeypatch so these run on any platform.
"""

from unittest.mock import MagicMock

import pytest

from commands import control_node_command as mod
from commands.control_node_command import ControlNodeCommand
from core.request_information import RequestInformation


@pytest.fixture
def cmd():
    return ControlNodeCommand()


@pytest.fixture
def mock_request_info():
    return MagicMock(spec=RequestInformation)


class TestProperties:
    def test_command_name(self, cmd):
        assert cmd.command_name == "control_node"

    def test_keywords(self, cmd):
        kw = cmd.keywords
        assert "volume" in kw
        assert "mute" in kw
        assert "unmute" in kw

    def test_action_param_enum(self, cmd):
        action_param = next(p for p in cmd.parameters if p.name == "action")
        assert action_param.required is True
        assert set(action_param.enum_values) == {
            "volume_up", "volume_down", "set_volume", "mute", "unmute",
        }

    def test_target_percent_optional(self, cmd):
        param = next(p for p in cmd.parameters if p.name == "target_percent")
        assert param.required is False

    def test_no_secrets(self, cmd):
        assert cmd.required_secrets == []


class TestPreRouteVolumeUpDown:
    @pytest.mark.parametrize("phrase", [
        "volume up",
        "Volume up",
        "turn the volume up",
        "turn it up",
        "louder",
        "crank it up",
    ])
    def test_volume_up(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "volume_up"}

    @pytest.mark.parametrize("phrase", [
        "volume down",
        "turn the volume down",
        "turn it down",
        "quieter",
        "softer",
    ])
    def test_volume_down(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "volume_down"}


class TestPreRouteSetVolume:
    """0-10 scale for bare numbers ≤ 10; literal percent otherwise."""

    @pytest.mark.parametrize("phrase,expected", [
        ("set the volume to 5", 50),
        ("set volume to 7", 70),
        ("change volume to 3", 30),
        ("set volume to 10", 100),     # bare 10 → 100% (boundary)
        ("set volume to 1", 10),       # bare 1 → 10%
        ("set the volume to 0", 0),    # bare 0 → 0%
    ])
    def test_bare_zero_to_ten_scale(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments["action"] == "set_volume"
        assert result.arguments["target_percent"] == expected

    @pytest.mark.parametrize("phrase,expected", [
        ("set the volume to 5%", 5),
        ("set the volume to 50%", 50),
        ("change volume to 100%", 100),
        ("set volume to 200%", 100),   # clamped
    ])
    def test_explicit_percent(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments["target_percent"] == expected

    @pytest.mark.parametrize("phrase,expected", [
        ("set the volume to 50", 50),  # bare > 10 → literal
        ("set volume to 75", 75),
        ("set the volume to 11", 11),  # boundary above scale
        ("set volume to 200", 100),    # clamped
    ])
    def test_bare_above_ten_literal(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments["target_percent"] == expected

    def test_bare_volume_with_number(self, cmd):
        # "Volume 7" still maps via 0-10 scale.
        result = cmd.pre_route("Volume 7")
        assert result is not None
        assert result.arguments == {"action": "set_volume", "target_percent": 70}

    def test_volume_up_does_not_match_bare_digit(self, cmd):
        # The up/down patterns must take precedence so "volume up" is never
        # parsed as a digit. Regression guard for ordering of branches.
        result = cmd.pre_route("volume up")
        assert result.arguments == {"action": "volume_up"}


class TestPreRouteMute:
    @pytest.mark.parametrize("phrase", [
        "mute", "Mute", "mute.", "please mute",
        "mute the volume", "mute the sound", "mute the speaker",
    ])
    def test_mute(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "mute"}

    @pytest.mark.parametrize("phrase", ["unmute", "unmute the volume", "unmute the speaker"])
    def test_unmute(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "unmute"}


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "what's the weather",
        "set a timer for 5 minutes",
        "turn on the kitchen light",
        "",
        "set the volume",  # missing number
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestRunVolumeUpDown:
    def test_volume_up(self, cmd, mock_request_info, monkeypatch):
        monkeypatch.setattr(mod, "adjust_volume_percent", lambda d: 60 if d == 10 else None)
        response = cmd.run(mock_request_info, action="volume_up")
        assert response.success is True
        assert response.context_data["volume_percent"] == 60

    def test_volume_down(self, cmd, mock_request_info, monkeypatch):
        seen = {}
        def fake(delta):
            seen["delta"] = delta
            return 40
        monkeypatch.setattr(mod, "adjust_volume_percent", fake)
        response = cmd.run(mock_request_info, action="volume_down")
        assert response.success is True
        assert seen["delta"] == -10
        assert response.context_data["volume_percent"] == 40

    def test_volume_up_unreadable(self, cmd, mock_request_info, monkeypatch):
        monkeypatch.setattr(mod, "adjust_volume_percent", lambda d: None)
        response = cmd.run(mock_request_info, action="volume_up")
        assert response.success is False


class TestRunSetVolume:
    def test_response_uses_user_target_not_alsa_readback(self, cmd, mock_request_info, monkeypatch):
        # Regression for the "set to 70 → speaks 71" round-trip drift bug.
        # The response must echo the value the user asked for, not whatever
        # comes back from ALSA after curving.
        monkeypatch.setattr(mod, "set_volume_percent", lambda p: True)
        response = cmd.run(mock_request_info, action="set_volume", target_percent=70)
        assert response.success is True
        assert response.context_data["volume_percent"] == 70
        assert "70 percent" in response.context_data["message"]

    def test_clamps_above_100(self, cmd, mock_request_info, monkeypatch):
        seen = {}
        def fake_set(p):
            seen["target"] = p
            return True
        monkeypatch.setattr(mod, "set_volume_percent", fake_set)
        response = cmd.run(mock_request_info, action="set_volume", target_percent=150)
        assert seen["target"] == 100
        assert response.success is True
        assert response.context_data["volume_percent"] == 100

    def test_clamps_below_zero(self, cmd, mock_request_info, monkeypatch):
        seen = {}
        monkeypatch.setattr(mod, "set_volume_percent", lambda p: seen.setdefault("t", p) or True)
        cmd.run(mock_request_info, action="set_volume", target_percent=-5)
        assert seen["t"] == 0

    def test_missing_target(self, cmd, mock_request_info):
        response = cmd.run(mock_request_info, action="set_volume")
        assert response.success is False
        assert "target_percent" in response.error_details

    def test_non_int_target(self, cmd, mock_request_info):
        response = cmd.run(mock_request_info, action="set_volume", target_percent="loud")
        assert response.success is False


class TestRunMute:
    def test_mute_calls_set_muted_true(self, cmd, mock_request_info, monkeypatch):
        seen = {}
        monkeypatch.setattr(mod, "set_muted", lambda m: seen.setdefault("muted", m) or True)
        response = cmd.run(mock_request_info, action="mute")
        assert response.success is True
        assert seen["muted"] is True

    def test_unmute_calls_set_muted_false(self, cmd, mock_request_info, monkeypatch):
        seen = {}
        monkeypatch.setattr(mod, "set_muted", lambda m: seen.setdefault("muted", m) or True)
        response = cmd.run(mock_request_info, action="unmute")
        assert response.success is True
        assert seen["muted"] is False

    def test_failure_propagates(self, cmd, mock_request_info, monkeypatch):
        monkeypatch.setattr(mod, "set_muted", lambda m: False)
        response = cmd.run(mock_request_info, action="mute")
        assert response.success is False


class TestRunUnknownAction:
    def test_unknown(self, cmd, mock_request_info):
        response = cmd.run(mock_request_info, action="explode")
        assert response.success is False
        assert "Unknown action" in response.error_details
