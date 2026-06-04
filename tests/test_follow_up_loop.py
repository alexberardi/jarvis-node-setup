"""Tests for the follow-up loop — post-TTS conversation continuation.

When the assistant finishes speaking, the loop listens for a few seconds
of follow-up speech and either continues the conversation or returns to
wake-word mode. Brittleness epicenter pre-refactor — 11 distinct exit
paths through four layers of guards plus a barge-in interruption.

The four guard layers in order:

  1. **Hard cap** — `iteration > max_iterations` exits regardless of
     audio. Prevents infinite loops on stuck input.
  2. **Decaying timeout** — `listen_for_follow_up` waits less each
     iteration; long silences eventually time out.
  3. **Follow-up noise** — short/repeated transcripts that slip past
     `is_non_speech`. Two consecutive noise hits exit.
  4. **Self-echo** — `looks_like_self_echo` against the last assistant
     reply. Hard exit, not consecutive — one confirmed echo is enough.

Plus result-driven exits (clear_history / not_for_me), the not_for_me
short-circuit at entry, and barge-in interruption during the response.

Test strategy: mock every external surface (bus, STT, command service,
BargeInMonitor, listen_for_follow_up, music duck). For each exit path,
construct the smallest sequence of inputs that drives the loop to it.
"""

from __future__ import annotations

from concurrent.futures import Future
from unittest.mock import MagicMock

import pytest

from core import follow_up_loop
from core.ijarvis_speech_to_text_provider import TranscriptionResult


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class FakeExecutor:
    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs) -> Future:
        self.submitted.append((fn, args, kwargs))
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as e:  # pragma: no cover
            f.set_exception(e)
        return f


@pytest.fixture(autouse=True)
def _isolate_loop(monkeypatch):
    """Per-test: install a fake bg_executor; stub the LED + music duck
    + barge-in helpers so the loop doesn't touch real hardware."""
    fake_exec = FakeExecutor()
    monkeypatch.setattr(follow_up_loop, "_bg_executor", fake_exec)
    monkeypatch.setattr(follow_up_loop, "set_led_transient", lambda p: None)
    monkeypatch.setattr(follow_up_loop, "pause_active_playback", lambda: None)
    # Default Config: follow-up enabled with a 4-second window.
    monkeypatch.setattr(
        follow_up_loop.Config, "get_float",
        lambda key, default: default if key != "follow_up_listen_seconds" else 4.0,
    )
    monkeypatch.setattr(
        follow_up_loop.Config, "get_int",
        lambda key, default: default,
    )
    # Echo + noise checks default to "no" so each test can opt-in.
    monkeypatch.setattr(follow_up_loop, "looks_like_self_echo",
                        lambda text, prev: False)
    monkeypatch.setattr(follow_up_loop, "is_follow_up_noise",
                        lambda text, prev: False)
    monkeypatch.setattr(follow_up_loop, "is_non_speech", lambda text: False)
    monkeypatch.setattr(follow_up_loop, "extract_assistant_text",
                        lambda result: "")
    # Barge-in not enabled by default; tests that want it install their own.
    monkeypatch.setattr(follow_up_loop, "barge_in_enabled", lambda: False)
    yield fake_exec


def _cs(returns: dict | None = None) -> MagicMock:
    """Make a command-service mock whose process_voice_command +
    continue_conversation return ``returns`` and whose try_pre_route
    is unmatched by default."""
    cs = MagicMock()
    cs.try_pre_route = MagicMock(return_value=None)
    cs.process_voice_command = MagicMock(return_value=returns or {})
    cs.continue_conversation = MagicMock(return_value=returns or {})
    cs.speak_result = MagicMock()
    cs.client = MagicMock()
    cs.client.end_conversation = MagicMock()
    return cs


def _stt(text: str = "", speaker_user_id: int | None = None) -> MagicMock:
    stt = MagicMock()
    stt.transcribe_with_speaker = MagicMock(
        return_value=TranscriptionResult(text=text, speaker_user_id=speaker_user_id),
    )
    return stt


def _listen_returning(*paths_then_none):
    """Build a listen_for_follow_up replacement that yields the given
    paths in order then None forever. Lets a test simulate "user spoke
    twice, then silence" with three calls."""
    seq = list(paths_then_none)
    seq.append(None)  # sentinel so an extra iteration safely terminates

    def _listen(*a, **kw):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return _listen


# ---------------------------------------------------------------------------
# Entry-time guards
# ---------------------------------------------------------------------------


class TestEntryGuards:

    def test_follow_up_disabled_returns_immediately(self, monkeypatch):
        # follow_up_listen_seconds <= 0 → no-op.
        monkeypatch.setattr(
            follow_up_loop.Config, "get_float",
            lambda key, default: 0.0 if key == "follow_up_listen_seconds" else default,
        )
        listen_called: list = []
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            lambda *a, **kw: listen_called.append(True),
        )
        cs = _cs()
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        assert listen_called == []
        # No end_conversation call — we never entered the loop.
        cs.client.end_conversation.assert_not_called()

    def test_initial_not_for_me_skips_follow_up(self, monkeypatch):
        # A wake the LLM determined wasn't for us — don't continue listening.
        listen_called: list = []
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            lambda *a, **kw: listen_called.append(True),
        )
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"not_for_me": True, "conversation_id": "c1"},
            command_service=_cs(),
            stt_provider=_stt(),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        assert listen_called == []


# ---------------------------------------------------------------------------
# Per-iteration exits
# ---------------------------------------------------------------------------


class TestPerIterationExits:

    def test_listen_timeout_breaks_first_iteration(self, monkeypatch):
        # listen returns None → window expired → break, no STT call.
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            lambda *a, **kw: None,
        )
        stt = _stt()
        cs = _cs({"success": True, "conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        stt.transcribe_with_speaker.assert_not_called()
        cs.client.end_conversation.assert_called_once_with("c1")

    def test_stt_exception_breaks(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        stt = MagicMock()
        stt.transcribe_with_speaker = MagicMock(side_effect=RuntimeError("stt boom"))
        cs = _cs({"success": True, "conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=stt,
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        # STT was attempted, exception caught, loop broke out → no CC call.
        cs.process_voice_command.assert_not_called()
        cs.continue_conversation.assert_not_called()

    def test_non_speech_breaks(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        monkeypatch.setattr(follow_up_loop, "is_non_speech", lambda text: True)
        cs = _cs()
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="[BLANK_AUDIO]"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        cs.continue_conversation.assert_not_called()


class TestNoiseGuard:
    """Layer 3 — two consecutive noise transcripts ends the follow-up."""

    def test_single_noise_then_real_speech_continues(self, monkeypatch):
        # First iteration: noise. Second: real speech. CC sees the real
        # speech only; the noise iteration is skipped without a command.
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav", "/tmp/b.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        noise_seq = [True, False]  # noise once, then not
        monkeypatch.setattr(
            follow_up_loop, "is_follow_up_noise",
            lambda text, prev: noise_seq.pop(0) if noise_seq else False,
        )
        # The second iteration must produce a result with clear_history
        # so the loop exits cleanly instead of looping forever.
        cs = _cs({"clear_history": True, "conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="real speech"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        # Continue_conversation was called once (second iteration only).
        cs.continue_conversation.assert_called_once()

    def test_two_consecutive_noise_breaks(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav", "/tmp/b.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        monkeypatch.setattr(
            follow_up_loop, "is_follow_up_noise",
            lambda text, prev: True,
        )
        cs = _cs()
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="noise"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        # Two noise iterations, no real command processed.
        cs.continue_conversation.assert_not_called()
        cs.process_voice_command.assert_not_called()


class TestSelfEchoGuard:
    """Layer 4 — single self-echo hit ends the follow-up (hard exit)."""

    def test_self_echo_breaks_immediately(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        monkeypatch.setattr(
            follow_up_loop, "looks_like_self_echo",
            lambda text, prev: True,
        )
        cs = _cs()
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="Here is sad music"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        # Even one echo → break, no CC round-trip.
        cs.continue_conversation.assert_not_called()


class TestMaxIterationsCap:

    def test_max_iterations_breaks_loop(self, monkeypatch):
        # Set max_follow_up_iterations to 1 so iter 2 trips the cap.
        # Listen never times out; the only exit is the hard cap.
        monkeypatch.setattr(
            follow_up_loop.Config, "get_int",
            lambda key, default: 1 if key == "max_follow_up_iterations" else default,
        )
        listen_calls: list = []
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            lambda *a, **kw: listen_calls.append(True) or "/tmp/a.wav",
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        # First (and only) iteration: CC returns a result that does NOT
        # set clear_history or not_for_me, so the loop would otherwise
        # continue. The cap is what stops it.
        cs = _cs({"conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="say more"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        # Exactly one listen call — iteration 2 trips the cap before
        # asking the bus.
        assert len(listen_calls) == 1


# ---------------------------------------------------------------------------
# CC-result-driven exits
# ---------------------------------------------------------------------------


class TestResultDrivenExits:

    def test_clear_history_breaks(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({"clear_history": True, "conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="ok thanks"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        # Exactly one CC call — no second iteration after clear_history.
        cs.continue_conversation.assert_called_once()

    def test_not_for_me_in_result_breaks(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({"not_for_me": True, "conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="say more"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        cs.continue_conversation.assert_called_once()

    def test_cc_exception_breaks(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs()
        cs.continue_conversation = MagicMock(side_effect=RuntimeError("cc boom"))
        # Should not raise out of the loop.
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="hi"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )


# ---------------------------------------------------------------------------
# CC path dispatch — pre-route vs continue vs fresh
# ---------------------------------------------------------------------------


class TestCcPathDispatch:

    def test_pre_route_match_skips_cc(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({"conversation_id": "c1"})
        # Pre-route matched ("stop", "pause", etc.) → speak_result with
        # pre_result, conversation_id cleared, NOT continue_conversation.
        cs.try_pre_route = MagicMock(return_value={"ok": True})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="stop"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        cs.try_pre_route.assert_called()
        cs.speak_result.assert_called_with({"ok": True})
        cs.continue_conversation.assert_not_called()
        cs.process_voice_command.assert_not_called()
        # end_conversation should NOT fire — pre-route cleared conv_id.
        cs.client.end_conversation.assert_not_called()

    def test_no_conv_id_uses_process_voice_command(self, monkeypatch):
        # Initial result had no conversation_id (e.g. command failed or
        # first turn was a one-shot) — fresh process_voice_command path.
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({"clear_history": True, "conversation_id": "c2"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result=None,  # no conversation_id seeded
            command_service=cs,
            stt_provider=_stt(text="hello again"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        cs.process_voice_command.assert_called_once()
        cs.continue_conversation.assert_not_called()

    def test_with_conv_id_uses_continue_conversation(self, monkeypatch):
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({"clear_history": True, "conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="continue please"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        cs.continue_conversation.assert_called_once()
        cs.process_voice_command.assert_not_called()


# ---------------------------------------------------------------------------
# Barge-in
# ---------------------------------------------------------------------------


class TestBargeIn:

    def test_barge_in_interrupted_resets_audio(self, monkeypatch):
        # Barge-in enabled, monitor reports interrupted → break + reset.
        monkeypatch.setattr(follow_up_loop, "barge_in_enabled", lambda: True)
        monkeypatch.setattr(follow_up_loop, "barge_in_threshold", lambda: 0.1)
        monkeypatch.setattr(
            follow_up_loop, "barge_in_energy_threshold", lambda: 500.0,
        )

        fake_monitor = MagicMock()
        fake_monitor.was_interrupted = True
        fake_monitor.start = MagicMock()
        fake_monitor.stop = MagicMock()
        monkeypatch.setattr(
            follow_up_loop, "BargeInMonitor",
            lambda *a, **kw: fake_monitor,
        )

        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        reset_calls: list = []
        monkeypatch.setattr(
            follow_up_loop.platform_audio, "reset_cancel",
            lambda: reset_calls.append(True),
        )

        cs = _cs({"conversation_id": "c1"})
        oww = MagicMock()
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="hi"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
            oww=oww,
        )
        fake_monitor.start.assert_called_once()
        fake_monitor.stop.assert_called_once()
        assert reset_calls == [True]


# ---------------------------------------------------------------------------
# end_conversation side effect
# ---------------------------------------------------------------------------


class TestEndOfLoopCleanup:

    def test_end_conversation_called_when_conv_id_present(self, monkeypatch):
        # Loop exited cleanly with a conversation_id still in scope —
        # tell CC to drop per-node speaker stickiness.
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({"clear_history": True, "conversation_id": "c1"})
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="goodbye"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        cs.client.end_conversation.assert_called_once_with("c1")

    def test_end_conversation_failure_is_swallowed(self, monkeypatch):
        # end_conversation raising should not bubble out of the loop —
        # the wake cycle is done either way.
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs({"clear_history": True, "conversation_id": "c1"})
        cs.client.end_conversation = MagicMock(side_effect=OSError("cc down"))
        # Must not raise.
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="goodbye"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )

    def test_no_end_conversation_when_conv_id_cleared(self, monkeypatch):
        # Pre-route clears conversation_id → end_conversation NOT called
        # because there's no conversation to end.
        monkeypatch.setattr(
            follow_up_loop, "listen_for_follow_up",
            _listen_returning("/tmp/a.wav"),
        )
        monkeypatch.setattr(
            follow_up_loop, "try_build_speaker_audio",
            lambda wake, cmd: None,
        )
        cs = _cs()
        cs.try_pre_route = MagicMock(return_value={"ok": True})
        # After pre-route, conv_id is None — next iter takes the
        # process_voice_command path with a clear_history result that
        # has no conv_id either.
        cs.process_voice_command = MagicMock(
            return_value={"clear_history": True}  # no conversation_id
        )
        follow_up_loop.follow_up_loop(
            bus=MagicMock(),
            initial_result={"success": True, "conversation_id": "c1"},
            command_service=cs,
            stt_provider=_stt(text="stop"),
            validation_handler=lambda v: "",
            wake_word_model="hey_jarvis",
        )
        # Loop ended with conv_id=None → end_conversation skipped.
        cs.client.end_conversation.assert_not_called()


# ---------------------------------------------------------------------------
# set_runtime — DI hook the wake loop uses to inject bg_executor
# ---------------------------------------------------------------------------


class TestSetRuntime:

    def test_installs_bg_executor(self):
        sentinel = MagicMock(name="executor")
        follow_up_loop.set_runtime(bg_executor=sentinel)
        assert follow_up_loop._bg_executor is sentinel
