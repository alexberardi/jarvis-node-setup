"""end_of_exchange: CC says the exchange is over → the node stops listening.

CC strips the model's <exchange_complete/> marker and sets
``end_of_exchange`` on the response (jarvis-command-center#89). The node's
job: skip the follow-up window entirely on an initial terminal reply, and
break out of the window when a follow-up turn lands terminal. Sitting
quietly is the designed ending; the user can always re-wake.
"""

from unittest.mock import MagicMock

import pytest

from clients.responses.jarvis_command_center import ToolCallingResponse
from core import follow_up_loop
from tests.test_follow_up_loop import _cs, _listen_returning, _stt


class TestResponseModel:
    def test_parses_flag(self):
        r = ToolCallingResponse.model_validate(
            {"stop_reason": "complete", "assistant_message": "Goodnight!",
             "end_of_exchange": True}
        )
        assert r.end_of_exchange is True

    def test_defaults_false_for_old_cc(self):
        r = ToolCallingResponse.model_validate(
            {"stop_reason": "complete", "assistant_message": "Hi"}
        )
        assert r.end_of_exchange is False


class TestConversationLoopPropagation:
    def test_final_result_carries_flag(self):
        from utils.command_execution_service import CommandExecutionService

        service = CommandExecutionService.__new__(CommandExecutionService)
        service.client = MagicMock()
        response = ToolCallingResponse.model_validate(
            {"stop_reason": "complete", "assistant_message": "Timer set.",
             "end_of_exchange": True}
        )
        result = service._run_conversation_loop(response, "conv-1", None)
        assert result["end_of_exchange"] is True

    def test_final_result_flag_defaults_false(self):
        from utils.command_execution_service import CommandExecutionService

        service = CommandExecutionService.__new__(CommandExecutionService)
        service.client = MagicMock()
        response = ToolCallingResponse.model_validate(
            {"stop_reason": "complete", "assistant_message": "72 and sunny."}
        )
        result = service._run_conversation_loop(response, "conv-1", None)
        assert result.get("end_of_exchange", False) is False


class TestFollowUpWindow:
    def test_initial_end_of_exchange_skips_window(self, monkeypatch):
        # Terminal initial reply ("Goodnight!") — never open the mic.
        listen_called: list = []
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            lambda *a, **kw: listen_called.append(True),
        )
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={
                "success": True, "conversation_id": "c1",
                "end_of_exchange": True,
            },
            command_service=_cs(),
            stt_provider=_stt(),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        assert listen_called == []

    def test_follow_up_end_of_exchange_breaks(self, monkeypatch):
        # A follow-up turn that lands terminal closes the window — exactly
        # one CC call, no re-listen.
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({
            "success": True, "conversation_id": "c1",
            "end_of_exchange": True,
        })
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="thanks, goodnight"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        cs.continue_conversation.assert_called_once()
