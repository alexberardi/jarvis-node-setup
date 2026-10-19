"""The node's timer, audio-control, alert-queue, CC HTTP and LLM SDK backends.

Each backend forwards to the node service it wraps, and
``init_command_backends()`` (called by scripts/main.py and text_mode.py)
registers all five with the SDK.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jarvis_command_sdk import (
    alerts_count,
    alerts_flush,
    alerts_pending,
    alerts_remove,
    audio_adjust_volume_percent,
    audio_set_muted,
    audio_set_volume_percent,
    audio_speak,
    cc_post,
    llm_chat_text,
    timer_cancel,
    timer_clear_all,
    timer_find_by_label,
    timer_get,
    timer_list_active,
    timer_set,
)
from jarvis_command_sdk.alert_queue import get_alert_queue_backend, set_alert_queue_backend
from jarvis_command_sdk.audio_control import get_audio_control_backend, set_audio_control_backend
from jarvis_command_sdk.cc_http import get_cc_backend, set_cc_backend
from jarvis_command_sdk.llm import get_llm_backend, set_llm_backend
from jarvis_command_sdk.timer import get_timer_backend, set_timer_backend

from services.alert_queue_backend import NodeAlertQueueBackend
from services.audio_control_backend import NodeAudioControlBackend
from services.cc_backend import NodeCCHttpBackend, NodeLLMBackend
from services.command_backends import init_command_backends
from services.timer_backend import NodeTimerBackend

REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _reset_backends():
    yield
    for setter in (set_timer_backend, set_audio_control_backend,
                   set_alert_queue_backend, set_cc_backend, set_llm_backend):
        setter(None)


class TestInit:
    def test_registers_all_five(self):
        for getter in (get_timer_backend, get_audio_control_backend,
                       get_alert_queue_backend, get_cc_backend, get_llm_backend):
            assert getter() is None
        assert init_command_backends() == []
        assert isinstance(get_timer_backend(), NodeTimerBackend)
        assert isinstance(get_audio_control_backend(), NodeAudioControlBackend)
        assert isinstance(get_alert_queue_backend(), NodeAlertQueueBackend)
        assert isinstance(get_cc_backend(), NodeCCHttpBackend)
        assert isinstance(get_llm_backend(), NodeLLMBackend)

    def test_one_failure_does_not_stop_the_others(self):
        with patch("services.timer_backend.init_timer_backend", side_effect=RuntimeError("boom")):
            assert init_command_backends() == ["timer"]
        assert get_timer_backend() is None
        assert isinstance(get_llm_backend(), NodeLLMBackend)

    def test_registers_without_the_http_client_importable(self):
        # CI installs no `requests`: registering must not import the node clients.
        code = (
            "import sys; sys.modules['requests'] = None\n"
            "from services.command_backends import init_command_backends\n"
            "print(init_command_backends())\n"
        )
        out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT,
                             capture_output=True, text=True, timeout=60)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip().splitlines()[-1] == "[]"

    @pytest.mark.parametrize("script", ["scripts/main.py", "scripts/text_mode.py"])
    def test_startup_scripts_register_them(self, script):
        assert "init_command_backends()" in (REPO_ROOT / script).read_text()


class TestTimerBackend:
    @pytest.fixture
    def svc(self):
        svc = MagicMock()
        with patch("services.timer_service.get_timer_service", return_value=svc):
            set_timer_backend(NodeTimerBackend())
            yield svc

    def test_delegates(self, svc):
        svc.set_timer.return_value = "t1"
        svc.cancel_timer.return_value = True
        svc.get_active_timers.return_value = [{"timer_id": "t1"}]
        svc.get_timer.return_value = {"timer_id": "t1", "label": "pasta"}
        svc.find_timer_by_label.return_value = "t1"
        svc.clear_all.return_value = 2

        assert timer_set(60, "pasta") == "t1"
        svc.set_timer.assert_called_once_with(60, "pasta")
        assert timer_cancel("t1") is True
        svc.cancel_timer.assert_called_once_with("t1")
        assert timer_list_active() == [{"timer_id": "t1"}]
        assert timer_get("t1") == {"timer_id": "t1", "label": "pasta"}
        svc.get_timer.assert_called_once_with("t1")
        assert timer_find_by_label("pasta") == "t1"
        svc.find_timer_by_label.assert_called_once_with("pasta")
        assert timer_clear_all() == 2

    def test_real_service_round_trip(self):
        from services.timer_service import TimerService

        TimerService._instance = None
        set_timer_backend(NodeTimerBackend())
        try:
            tid = timer_set(600, "pasta")
            assert tid is not None
            assert timer_find_by_label("pasta") == tid
            assert timer_get(tid)["label"] == "pasta"
            assert [t["timer_id"] for t in timer_list_active()] == [tid]
            assert timer_cancel(tid) is True
            assert timer_list_active() == []
        finally:
            TimerService().clear_all()
            TimerService._instance = None


class TestAudioControlBackend:
    def test_delegates_volume_and_mute(self):
        set_audio_control_backend(NodeAudioControlBackend())
        with patch("utils.audio_volume.set_volume_percent", return_value=True) as sv, \
             patch("utils.audio_volume.adjust_volume_percent", return_value=55) as av, \
             patch("utils.audio_volume.set_muted", return_value=True) as sm:
            assert audio_set_volume_percent(40) is True
            sv.assert_called_once_with(40)
            assert audio_adjust_volume_percent(-10) == 55
            av.assert_called_once_with(-10)
            assert audio_set_muted(True) is True
            sm.assert_called_once_with(True)

    def test_speak_uses_tts_provider(self):
        set_audio_control_backend(NodeAudioControlBackend())
        tts = MagicMock()
        with patch("core.helpers.get_tts_provider", return_value=tts):
            assert audio_speak("hello") is True
        tts.speak.assert_called_once_with(False, "hello")

    def test_speak_failure_is_false(self):
        set_audio_control_backend(NodeAudioControlBackend())
        with patch("core.helpers.get_tts_provider", side_effect=RuntimeError("no tts")):
            assert audio_speak("hello") is False


class TestAlertQueueBackend:
    def test_delegates(self):
        queue = MagicMock()
        queue.get_pending.return_value = ["a"]
        queue.flush.return_value = ["a", "b"]
        queue.remove_ids.return_value = 1
        queue.count.return_value = 3
        set_alert_queue_backend(NodeAlertQueueBackend())
        with patch("services.alert_queue_service.get_alert_queue_service", return_value=queue):
            assert alerts_pending() == ["a"]
            assert alerts_flush() == ["a", "b"]
            assert alerts_remove(iter(["x"])) == 1
            queue.remove_ids.assert_called_once_with(["x"])
            assert alerts_count() == 3


class TestCCBackends:
    def test_post_resolves_url_and_uses_rest_client(self):
        set_cc_backend(NodeCCHttpBackend())
        with patch("utils.service_discovery.get_command_center_url", return_value="http://cc:7703/"), \
             patch("clients.rest_client.RestClient.post", return_value={"ok": True}) as post:
            assert cc_post("/api/v0/x", {"k": 1}) == {"ok": True}
        post.assert_called_once_with("http://cc:7703/api/v0/x", data={"k": 1})

    def test_post_without_cc_url_is_none(self):
        set_cc_backend(NodeCCHttpBackend())
        with patch("utils.service_discovery.get_command_center_url", return_value=None), \
             patch("clients.rest_client.RestClient.post") as post:
            assert cc_post("/api/v0/x", {}) is None
        post.assert_not_called()

    def test_chat_text_uses_cc_client(self):
        set_llm_backend(NodeLLMBackend())
        with patch("utils.service_discovery.get_command_center_url", return_value="http://cc:7703"), \
             patch("clients.jarvis_command_center_client.JarvisCommandCenterClient") as cls:
            cls.return_value.chat_text.return_value = "hi"
            assert llm_chat_text("prompt") == "hi"
        cls.assert_called_once_with("http://cc:7703")
        cls.return_value.chat_text.assert_called_once_with("prompt")

    def test_chat_text_failure_is_none(self):
        set_llm_backend(NodeLLMBackend())
        with patch("utils.service_discovery.get_command_center_url", return_value="http://cc:7703"), \
             patch("clients.jarvis_command_center_client.JarvisCommandCenterClient") as cls:
            cls.return_value.chat_text.side_effect = RuntimeError("down")
            assert llm_chat_text("prompt") is None
