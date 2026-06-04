"""Tests for the main wake loop — outer iteration setup, inner-chunk
scoring, the wake-fire pipeline, the alert-drain break-out, and the
post-fire orchestration chain (handle_keyword_detected → listen →
send_for_transcription → follow_up_loop).

Test strategy: replace every external surface with a mock, then drive
the loop by feeding prepared audio chunks and OWW prediction scores
into a fake bus + fake OWW model. Each test forces an exit via either
KeyboardInterrupt (raised from a callable inside the loop body) or
RuntimeError, since the production loop has no natural termination.
"""

from __future__ import annotations

import queue
import threading
from concurrent.futures import Future
from unittest.mock import MagicMock

import numpy as np
import pytest

from core import wake_loop


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Synchronous executor — runs submitted callables inline so tests
    don't race on background threads. Records what was submitted."""

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


class FakeBus:
    """Mock AudioBus — yields prepared chunk bytes via a Queue."""

    def __init__(self, rate: int = 48000, chunks=None) -> None:
        self.rate = rate
        self._q: queue.Queue = queue.Queue()
        for c in (chunks or []):
            self._q.put(c)
        self.subscribe_calls: list[str] = []
        self.unsubscribe_calls: list[str] = []

    def subscribe(self, tag: str) -> queue.Queue:
        self.subscribe_calls.append(tag)
        return self._q

    def unsubscribe(self, tag: str) -> None:
        self.unsubscribe_calls.append(tag)

    def stop(self) -> None:
        pass


class FakeOWW:
    """Mock openWakeWord model — returns prepared scores in sequence."""

    def __init__(self, scores=None, wake_word_model: str = "hey_jarvis") -> None:
        self._scores = list(scores or [])
        self._key = wake_word_model
        self.reset_count = 0

    def predict(self, samples) -> dict:
        score = self._scores.pop(0) if self._scores else 0.0
        return {self._key: score}

    def reset(self) -> None:
        self.reset_count += 1


def _chunk_bytes(samples: int = 3840) -> bytes:
    """One 80 ms chunk at 48 kHz mono int16."""
    return np.zeros(samples, dtype=np.int16).tobytes()


@pytest.fixture(autouse=True)
def _isolate_loop(monkeypatch):
    """Replace every external surface with a mock so the loop can run
    against an in-memory bus + OWW without touching real hardware."""
    fake_exec = FakeExecutor()
    paused = threading.Event()
    monkeypatch.setattr(wake_loop, "_bg_executor", fake_exec)
    monkeypatch.setattr(wake_loop, "_wake_paused", paused)

    # Threshold + config helpers
    monkeypatch.setattr(wake_loop, "current_wake_threshold", lambda: 0.5)
    monkeypatch.setattr(wake_loop, "music_is_playing", lambda: False)
    monkeypatch.setattr(wake_loop, "wake_music_energy_multiplier", lambda: 1.0)
    monkeypatch.setattr(wake_loop, "_pre_wake_vad_threshold", lambda: 500.0)
    monkeypatch.setattr(
        wake_loop, "_adaptive_silence_threshold", lambda stats: None
    )

    # Make decide_wake_fire delegate the score check — score > threshold
    # is the simplest characterization of the production gate. The full
    # three-gate logic is already tested in test_wake_detector.py.
    class _Verdict:
        def __init__(self, should_fire: bool) -> None:
            self.should_fire = should_fire

    def _decide(*, score, static_wake_threshold, **kw):
        return _Verdict(score >= static_wake_threshold)

    monkeypatch.setattr(wake_loop, "decide_wake_fire", _decide)

    # Stub downstream side effects
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", lambda: None)
    monkeypatch.setattr(wake_loop, "play_processing_ack", lambda: False)
    monkeypatch.setattr(wake_loop, "set_led_transient", lambda x: None)
    monkeypatch.setattr(wake_loop, "fetch_next_processing_ack", lambda: None)
    monkeypatch.setattr(wake_loop, "pause_active_playback", lambda: None)
    monkeypatch.setattr(wake_loop, "resume_active_playback", lambda: None)
    monkeypatch.setattr(wake_loop, "drain_alert_announcements", MagicMock())
    monkeypatch.setattr(wake_loop, "follow_up_loop", MagicMock())
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", MagicMock())
    monkeypatch.setattr(wake_loop, "locked_oww_reset", lambda o: None)
    monkeypatch.setattr(wake_loop, "try_capture_wake_audio", lambda b: None)
    monkeypatch.setattr(wake_loop, "get_last_speaker", lambda: (None, None))
    monkeypatch.setattr(wake_loop, "run_warmup", lambda *a, **kw: None)
    monkeypatch.setattr(wake_loop, "listen", lambda *a, **kw: "/tmp/r.wav")
    monkeypatch.setattr(
        wake_loop, "send_for_transcription",
        lambda *a, **kw: {"text": "ok"},
    )

    # Alert-queue probe defaults to "nothing pending"
    monkeypatch.setattr(
        wake_loop, "has_pending_high_priority_alerts", lambda: False,
    )

    # Barge-in defaults disabled
    monkeypatch.setattr(wake_loop, "barge_in_enabled", lambda: False)
    monkeypatch.setattr(wake_loop, "barge_in_threshold", lambda: 0.5)
    monkeypatch.setattr(
        wake_loop, "barge_in_energy_threshold", lambda: 1000.0
    )
    monkeypatch.setattr(wake_loop, "BargeInMonitor", MagicMock())
    monkeypatch.setattr(wake_loop, "platform_audio", MagicMock())

    # Config: return defaults so calls don't raise
    monkeypatch.setattr(
        wake_loop.Config, "get_bool",
        classmethod(lambda cls, key, default=False: default),
    )
    monkeypatch.setattr(
        wake_loop.Config, "get_float",
        classmethod(lambda cls, key, default=0.0: default),
    )
    monkeypatch.setattr(
        wake_loop.Config, "get_str",
        classmethod(lambda cls, key, default=None: default),
    )

    # Skip resample to keep the chunk shape identical (saves loading scipy
    # in tests). The loop uses np.clip + astype so a 1:1 resample_down is
    # fine — set bus.rate == OWW_RATE so resample_down evaluates to 1.
    yield fake_exec


def _run(
    bus,
    oww,
    *,
    aec_pipeline=None,
    wake_word_model: str = "hey_jarvis",
    expect_exit: type = KeyboardInterrupt,
):
    """Call run_wake_loop expecting it to exit via expect_exit."""
    with pytest.raises(expect_exit):
        wake_loop.run_wake_loop(
            bus=bus,
            oww=oww,
            command_service=MagicMock(),
            stt_provider=MagicMock(),
            validation_handler=MagicMock(),
            aec_pipeline=aec_pipeline,
            wake_word_model=wake_word_model,
        )


# ---------------------------------------------------------------------------
# Loop lifecycle
# ---------------------------------------------------------------------------


def test_keyboard_interrupt_propagates_out_of_loop(monkeypatch):
    """KeyboardInterrupt raised inside the loop must propagate so the
    outer start_voice_listener can catch it and clean up."""
    monkeypatch.setattr(
        wake_loop, "handle_keyword_detected",
        MagicMock(side_effect=KeyboardInterrupt()),
    )
    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)


def test_low_score_does_not_fire_wake(monkeypatch):
    """Scores below threshold should never enter the wake-fire branch.
    Forces exit by having oww.predict raise KeyboardInterrupt after a
    few low-score scoring calls (oww.predict runs once per non-empty
    chunk pull)."""
    handle = MagicMock()
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", handle)

    class _ExitingOWW(FakeOWW):
        def __init__(self, scores):
            super().__init__(scores)
            self.predict_count = 0

        def predict(self, samples):
            self.predict_count += 1
            if self.predict_count >= 3:
                raise KeyboardInterrupt
            return super().predict(samples)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280) for _ in range(5)])
    oww = _ExitingOWW(scores=[0.1] * 5)
    _run(bus, oww)

    handle.assert_not_called()


def test_high_score_fires_wake_and_triggers_full_chain(monkeypatch):
    """Score >= threshold should bus.unsubscribe('wake'),
    handle_keyword_detected, listen, send_for_transcription, then
    follow_up_loop. record_legitimate_wake_score fires with the
    snapshot score."""
    handle = MagicMock()
    fu = MagicMock(side_effect=KeyboardInterrupt())  # exit after one cycle
    listen_mock = MagicMock(return_value="/tmp/cmd.wav")
    sft = MagicMock(return_value={"text": "hi"})
    record = MagicMock()
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", handle)
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)
    monkeypatch.setattr(wake_loop, "listen", listen_mock)
    monkeypatch.setattr(wake_loop, "send_for_transcription", sft)
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", record)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    assert "wake" in bus.unsubscribe_calls
    handle.assert_called_once()
    listen_mock.assert_called_once()
    sft.assert_called_once()
    fu.assert_called_once()
    record.assert_called_once()
    # Snapshot score is the float at the moment of the fire.
    args, _ = record.call_args
    assert args[0] == pytest.approx(0.9, abs=0.001)


def test_not_for_me_result_skips_calibration(monkeypatch):
    """A not_for_me=True verdict from CC should NOT feed the auto-calibrator
    (we don't want to lower the bar based on probabilistic misfires)."""
    monkeypatch.setattr(
        wake_loop, "send_for_transcription",
        lambda *a, **kw: {"text": "", "not_for_me": True},
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    record = MagicMock()
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", record)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    record.assert_not_called()


def test_legit_result_records_calibration_with_snapshot(monkeypatch):
    """Non-not_for_me result should record the score that produced
    the fire (NOT some later overwritten score)."""
    monkeypatch.setattr(
        wake_loop, "send_for_transcription",
        lambda *a, **kw: {"text": "hello", "tool_calls": []},
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    record = MagicMock()
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", record)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.85])
    _run(bus, oww)

    record.assert_called_once()
    args, _ = record.call_args
    assert args[0] == pytest.approx(0.85, abs=0.001)


# ---------------------------------------------------------------------------
# Alert-drain break-out
# ---------------------------------------------------------------------------


def test_alert_pending_breaks_inner_loop_and_drains(monkeypatch):
    """When a high-priority alert is pending and the alert-check counter
    rolls over, the inner chunk loop must break with score==0, and
    drain_alert_announcements must be called from the outer loop."""
    # Force the alert-check counter to fire on the very first chunk.
    monkeypatch.setattr(wake_loop, "_ALERT_CHECK_INTERVAL", 0)
    monkeypatch.setattr(
        wake_loop, "has_pending_high_priority_alerts", lambda: True,
    )

    drain = MagicMock(side_effect=KeyboardInterrupt())  # exit after one drain
    monkeypatch.setattr(wake_loop, "drain_alert_announcements", drain)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.0])
    _run(bus, oww)

    drain.assert_called_once()
    # bus.unsubscribe should still have been called (break path runs the
    # finally that unsubscribes).
    assert "wake" in bus.unsubscribe_calls


def test_no_pending_alerts_does_not_drain(monkeypatch):
    """When the alert queue is empty, the drain function is never called
    even if the alert-check counter rolls over many times."""
    monkeypatch.setattr(wake_loop, "_ALERT_CHECK_INTERVAL", 0)
    drain = MagicMock()
    monkeypatch.setattr(wake_loop, "drain_alert_announcements", drain)

    class _ExitingOWW(FakeOWW):
        def __init__(self, scores):
            super().__init__(scores)
            self.predict_count = 0

        def predict(self, samples):
            self.predict_count += 1
            if self.predict_count >= 3:
                raise KeyboardInterrupt
            return super().predict(samples)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280) for _ in range(5)])
    oww = _ExitingOWW(scores=[0.1] * 5)
    _run(bus, oww)

    drain.assert_not_called()


# ---------------------------------------------------------------------------
# Pause / resume
# ---------------------------------------------------------------------------


def test_wake_paused_skips_chunks_without_scoring(monkeypatch):
    """When _wake_paused is set, chunks must be dropped without ever
    calling oww.predict — the wake detector must not fire on paused
    audio (e.g., during voice-enrollment-via-MQTT). Exit via a Bus
    whose subscribe call also raises KeyboardInterrupt after enough
    chunks have been consumed."""
    paused = threading.Event()
    paused.set()
    monkeypatch.setattr(wake_loop, "_wake_paused", paused)

    class _PausedExitBus(FakeBus):
        """Counts chunk consumption via subscribe-returned queue's get,
        and raises KeyboardInterrupt once enough chunks have been pulled."""

        def __init__(self, *a, max_gets: int = 3, **kw):
            super().__init__(*a, **kw)
            self._max_gets = max_gets
            self._gets = 0

        def subscribe(self, tag):
            super().subscribe(tag)
            outer = self

            class _CountingQ:
                def get(self, timeout=None):
                    outer._gets += 1
                    if outer._gets > outer._max_gets:
                        raise KeyboardInterrupt
                    return outer._q.get(timeout=timeout)

            return _CountingQ()

    predict_calls = {"n": 0}

    class _CountingOWW(FakeOWW):
        def predict(self, samples):
            predict_calls["n"] += 1
            return super().predict(samples)

    bus = _PausedExitBus(
        rate=16000, chunks=[_chunk_bytes(1280) for _ in range(3)]
    )
    oww = _CountingOWW(scores=[0.9] * 3)
    _run(bus, oww)

    assert predict_calls["n"] == 0


def test_resume_after_pause_resets_oww(monkeypatch):
    """First chunk after pause-resume transition: oww.reset must fire
    so the LSTM state doesn't carry pre-pause context (which historically
    re-triggered wake events on the tail of a wake-response TTS)."""
    paused = threading.Event()
    paused.set()
    monkeypatch.setattr(wake_loop, "_wake_paused", paused)

    class _ResumeBus(FakeBus):
        """Returns a queue whose get() flips _wake_paused after N reads."""

        def subscribe(self, tag):
            super().subscribe(tag)
            outer = self
            ev = paused

            class _Q:
                def __init__(self):
                    self.n = 0

                def get(self, timeout=None):
                    self.n += 1
                    if self.n == 2:
                        ev.clear()        # resume on second chunk pull
                    if self.n > 4:
                        raise KeyboardInterrupt
                    return outer._q.get(timeout=timeout)

            return _Q()

    bus = _ResumeBus(rate=16000, chunks=[_chunk_bytes(1280) for _ in range(5)])
    oww = FakeOWW(scores=[0.1] * 5)
    _run(bus, oww)

    # At least one reset from the resume transition (was_paused branch).
    assert oww.reset_count >= 1


# ---------------------------------------------------------------------------
# Barge-in interruption
# ---------------------------------------------------------------------------


def test_barge_in_interrupted_skips_follow_up(monkeypatch):
    """When barge_in.was_interrupted is True after the wake-fire chain,
    follow_up_loop must NOT be called — the user cut us off; they'll
    say the wake word again when ready."""
    monkeypatch.setattr(wake_loop, "barge_in_enabled", lambda: True)
    bi_instance = MagicMock()
    bi_instance.was_interrupted = True
    bi_class = MagicMock(return_value=bi_instance)
    monkeypatch.setattr(wake_loop, "BargeInMonitor", bi_class)

    led = MagicMock()
    monkeypatch.setattr(wake_loop, "set_led_transient", led)

    fu = MagicMock()
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    # Exit via fetch_next_processing_ack at end of post-wake block.
    monkeypatch.setattr(
        wake_loop, "fetch_next_processing_ack",
        MagicMock(side_effect=KeyboardInterrupt()),
    )

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    fu.assert_not_called()
    # set_led_transient(None) is called in the interrupted branch to
    # clear the cyan "speaking" LED + in the finally; assert >=1 None call.
    none_calls = [c for c in led.call_args_list if c.args == (None,)]
    assert len(none_calls) >= 1


def test_barge_in_not_interrupted_runs_follow_up(monkeypatch):
    """When barge_in did not interrupt, follow_up_loop must run."""
    monkeypatch.setattr(wake_loop, "barge_in_enabled", lambda: True)
    bi_instance = MagicMock()
    bi_instance.was_interrupted = False
    bi_class = MagicMock(return_value=bi_instance)
    monkeypatch.setattr(wake_loop, "BargeInMonitor", bi_class)

    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    fu.assert_called_once()


# ---------------------------------------------------------------------------
# Exception handling in the inner try-block
# ---------------------------------------------------------------------------


def test_send_for_transcription_exception_falls_through_to_follow_up(monkeypatch):
    """If send_for_transcription raises, the loop logs and continues
    to the if/else; with barge-in disabled, follow_up_loop is invoked
    with result=None. Existing behavior — characterized, not changed."""
    monkeypatch.setattr(
        wake_loop, "send_for_transcription",
        MagicMock(side_effect=RuntimeError("STT down")),
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    fu.assert_called_once()
    args, kwargs = fu.call_args
    # result is positional arg 1 (after bus).
    assert args[1] is None
