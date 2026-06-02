"""Tests for the MQTT ``tool_call`` handler's ``voice_command`` plumbing.

CC now ships the user's raw phrase to the node so commands can inspect it
(e.g. spotify's playlist-intent detection). When the payload omits it, we
fall back to the ``[mobile chat]`` placeholder for backwards compatibility
with older CCs or non-chat tool-call origins (routines).
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# scripts/ isn't a package; add it to sys.path so we can import mqtt_tts_listener.
_SCRIPTS_DIR = str(Path(__file__).resolve().parent.parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


@pytest.fixture
def fake_command() -> MagicMock:
    cmd = MagicMock()
    response = MagicMock()
    response.success = True
    response.error_details = None
    response.context_data = {"action": "play", "message": "ok"}
    response.actions = []
    cmd.execute.return_value = response
    return cmd


def _invoke_handler(details: dict, fake_cmd: MagicMock) -> dict:
    """Drive ``handle_tool_call`` with mocks and return the captured
    ``RequestInformation`` kwargs.
    """
    import mqtt_tts_listener as mtl

    captured: dict = {}

    class _FakeRI:
        def __init__(self, **kw: object) -> None:
            captured.update(kw)
            for k, v in kw.items():
                setattr(self, k, v)

    discovery = MagicMock()
    discovery.get_all_commands.return_value = {"spotify": fake_cmd}

    with patch.object(mtl, "_post_tool_call_result"), \
         patch("jarvis_command_sdk.RequestInformation", _FakeRI), \
         patch("jarvis_command_sdk.context.set_current_user_id"), \
         patch("utils.command_execution_service._build_secrets", return_value={}), \
         patch("utils.command_discovery_service.get_command_discovery_service", return_value=discovery):
        mtl.handle_tool_call(details)

    return captured


def test_tool_call_uses_payload_voice_command(fake_command: MagicMock) -> None:
    details = {
        "command_name": "spotify",
        "arguments": {"action": "play", "query": "jungle night"},
        "tool_call_id": "tc-1",
        "reply_request_id": "req-1",
        "user_id": 1,
        "voice_command": "play my jungle night playlist",
    }

    captured = _invoke_handler(details, fake_command)

    assert captured["voice_command"] == "play my jungle night playlist"
    assert captured["user_id"] == 1


def test_tool_call_falls_back_to_placeholder_when_missing(fake_command: MagicMock) -> None:
    details = {
        "command_name": "spotify",
        "arguments": {"action": "play", "query": "jungle night"},
        "tool_call_id": "tc-2",
        "reply_request_id": "req-2",
        "user_id": 1,
    }

    captured = _invoke_handler(details, fake_command)

    assert captured["voice_command"] == "[mobile chat]"


def test_tool_call_falls_back_when_voice_command_empty(fake_command: MagicMock) -> None:
    details = {
        "command_name": "spotify",
        "arguments": {"action": "play", "query": "jungle night"},
        "tool_call_id": "tc-3",
        "reply_request_id": "req-3",
        "user_id": 1,
        "voice_command": "",
    }

    captured = _invoke_handler(details, fake_command)

    assert captured["voice_command"] == "[mobile chat]"
