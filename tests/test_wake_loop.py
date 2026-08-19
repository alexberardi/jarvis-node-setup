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


class _ExhaustedExitQueue(queue.Queue):
    """Blocking get on an exhausted queue raises KeyboardInterrupt.

    The real wake loop polls ``get(timeout=0.5)`` and continues forever on
    an idle mic. Tests prepare a FINITE chunk list, so an empty queue on a
    blocking get means the test's audio is fully consumed — exit the loop
    deterministically instead of spinning. (Before the drain-to-newest
    change, tests exited by raising from oww.predict after N calls; the
    drain collapses a queued burst into one predict, so those counts are
    never reached and the loop spun on the empty queue.) Non-blocking gets
    — the drain pass itself — keep normal queue.Empty semantics.
    """

    def get(self, block=True, timeout=None):
        try:
            return super().get(block=False)
        except queue.Empty:
            if block:
                raise KeyboardInterrupt
            raise


class FakeBus:
    """Mock AudioBus — yields prepared chunk bytes via a Queue."""

    def __init__(self, rate: int = 48000, chunks=None) -> None:
        self.rate = rate
        self._q: queue.Queue = _ExhaustedExitQueue()
        for c in (chunks or []):
            self._q.put(c)
        self.subscribe_calls: list[str] = []
        self.unsubscribe_calls: list[str] = []

    def subscribe(self, tag: str, maxsize: int | None = None) -> queue.Queue:
        # ``maxsize`` accepted to match AudioBus.subscribe (the wake loop
        # passes maxsize=4 for drain-to-newest); the fake's queue stays
        # unbounded — tests enqueue everything up front.
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
    monkeypatch.setattr(
        wake_loop, "current_wake_threshold_with_profile",
        lambda: (0.5, "normal"),
    )
    # Reset the tentative-wake rolling cool-down so cases are hermetic.
    monkeypatch.setattr(
        wake_loop, "_tentative_last_trigger_ts", float("-inf")
    )
    monkeypatch.setattr(wake_loop, "_pre_wake_vad_threshold", lambda: 500.0)
    monkeypatch.setattr(
        wake_loop, "_adaptive_silence_threshold", lambda stats: None
    )
    monkeypatch.setattr(
        wake_loop, "_auto_pre_wake_vad_threshold", lambda stats: None
    )

    # Make decide_wake_fire delegate the score check — score > threshold
    # is the simplest characterization of the production gate. The full
    # two-gate logic is already tested in test_wake_detector.py.
    class _Verdict:
        def __init__(self, should_fire: bool) -> None:
            self.should_fire = should_fire

    def _decide(*, score, threshold, **kw):
        return _Verdict(score >= threshold)

    monkeypatch.setattr(wake_loop, "decide_wake_fire", _decide)

    # Stub downstream side effects
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", lambda: None)
    monkeypatch.setattr(wake_loop, "play_processing_ack", lambda: False)
    monkeypatch.setattr(wake_loop, "set_led_transient", lambda x: None)
    monkeypatch.setattr(wake_loop, "fetch_next_processing_ack", lambda: None)
    monkeypatch.setattr(wake_loop, "pause_active_playback", lambda: None)
    monkeypatch.setattr(wake_loop, "resume_active_playback", lambda: None)
    # Self-playback flag defaults to "not playing" so tests are hermetic
    # against music_control's module state.
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: False)
    monkeypatch.setattr(wake_loop, "set_self_playing", lambda v, **kw: None)
    # Echo-cancel layer defaults to "inactive / no-op" so tests are
    # hermetic against core.echo_cancel module state.
    monkeypatch.setattr(wake_loop, "echo_cancel_is_active", lambda: False)
    monkeypatch.setattr(wake_loop, "note_echo_cancel_chunk", lambda rms: None)
    monkeypatch.setattr(wake_loop, "drain_alert_announcements", MagicMock())
    monkeypatch.setattr(wake_loop, "follow_up_loop", MagicMock())
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", MagicMock())
    monkeypatch.setattr(wake_loop, "locked_oww_reset", lambda o: None)
    monkeypatch.setattr(wake_loop, "try_capture_wake_audio", lambda b: None)
    monkeypatch.setattr(
        wake_loop, "try_capture_wake_audio_from_frames", lambda f, b: None
    )
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
    (we don't want to lower the bar based on probabilistic misfires) — and
    it MUST arm the soft wake cool-down so the same side conversation can't
    immediately re-fire wake."""
    from core import voice_filters

    monkeypatch.setattr(
        wake_loop, "send_for_transcription",
        lambda *a, **kw: {"text": "", "not_for_me": True},
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    record = MagicMock()
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", record)
    voice_filters.reset_wake_gate()

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    try:
        _run(bus, oww)

        record.assert_not_called()
        # Soft cool-down armed: gate pushed well past the 8s debounce, with
        # an override threshold so a decisive wake can still punch through.
        assert voice_filters.get_wake_gate_override_threshold() is not None
    finally:
        voice_filters.reset_wake_gate()


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

        def subscribe(self, tag, maxsize=None):
            super().subscribe(tag, maxsize=maxsize)
            outer = self

            class _CountingQ:
                def get(self, timeout=None):
                    outer._gets += 1
                    if outer._gets > outer._max_gets:
                        raise KeyboardInterrupt
                    return outer._q.get(timeout=timeout)

                def get_nowait(self):
                    # Drain-to-newest pass: plain Empty semantics, not
                    # counted toward the exit budget.
                    return outer._q.get(block=False)

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

        def subscribe(self, tag, maxsize=None):
            super().subscribe(tag, maxsize=maxsize)
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

                def get_nowait(self):
                    # Refuse to drain: this test depends on one chunk per
                    # blocking get so the pause→resume schedule (flip on
                    # n==2, oww.reset on the first post-resume chunk)
                    # plays out — a real drain would consume the whole
                    # burst during the paused first iteration.
                    raise queue.Empty

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


# ---------------------------------------------------------------------------
# Wake-clip capture — consumed-chunks primary path + bus-snapshot fallback
# ---------------------------------------------------------------------------


def _distinct_chunk(value: int, samples: int = 1280) -> bytes:
    """One 80 ms chunk whose samples are all ``value`` — distinguishable
    from its siblings so clip-content assertions can check ordering."""
    return np.full(samples, value, dtype=np.int16).tobytes()


def test_wake_clip_built_from_consumed_chunks_including_drained(monkeypatch):
    """The wake clip must be written from EVERY chunk the loop pulled off
    its queue — including chunks the drain-to-newest pass skipped scoring.
    This is the keystone of the clip fix: by construction the clip equals
    the audio stream around the fire, so producer catch-up bursts can
    never evict the wake phrase the way the ring-buffer snapshot allowed.
    """
    chunks = [_distinct_chunk(v) for v in (1, 2, 3, 4)]
    captured: dict = {}

    def _capture(frames, bus):
        captured["frames"] = list(frames)
        return "/tmp/wake.wav"

    monkeypatch.setattr(
        wake_loop, "try_capture_wake_audio_from_frames", _capture,
    )
    fallback = MagicMock(return_value="/tmp/fallback.wav")
    monkeypatch.setattr(wake_loop, "try_capture_wake_audio", fallback)
    sft = MagicMock(return_value={"text": "hi"})
    monkeypatch.setattr(wake_loop, "send_for_transcription", sft)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    # All four chunks are enqueued up front: the first blocking get pulls
    # chunk 1, the drain pass pulls 2-4, and OWW scores only the newest.
    bus = FakeBus(rate=16000, chunks=chunks)
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    assert captured["frames"] == chunks  # drain-skipped chunks included
    fallback.assert_not_called()         # primary path succeeded
    assert sft.call_args.kwargs["wake_audio_path"] == "/tmp/wake.wav"


def test_wake_clip_falls_back_to_bus_snapshot(monkeypatch):
    """If the consumed-chunks write fails (returns None), the bus-ring
    snapshot must still be attempted and its path used downstream."""
    monkeypatch.setattr(
        wake_loop, "try_capture_wake_audio_from_frames",
        lambda frames, bus: None,
    )
    fallback = MagicMock(return_value="/tmp/fallback.wav")
    monkeypatch.setattr(wake_loop, "try_capture_wake_audio", fallback)
    sft = MagicMock(return_value={"text": "hi"})
    monkeypatch.setattr(wake_loop, "send_for_transcription", sft)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    fallback.assert_called_once()
    assert sft.call_args.kwargs["wake_audio_path"] == "/tmp/fallback.wav"


def test_wake_clip_deque_keeps_only_last_two_seconds(monkeypatch):
    """The clip deque is bounded to ~WAKE_CLIP_SECONDS of chunks (25 at
    80 ms) — older chunks roll off so the clip stays wake-phrase-sized."""
    n = 30
    chunks = [_distinct_chunk(v + 1) for v in range(n)]
    captured: dict = {}

    def _capture(frames, bus):
        captured["frames"] = list(frames)
        return "/tmp/wake.wav"

    monkeypatch.setattr(
        wake_loop, "try_capture_wake_audio_from_frames", _capture,
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=chunks)
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    # FakeBus exposes no chunk_samples, so the loop sizes the deque off
    # the OWW frame length: 2.0 s / 0.08 s = 25 chunks, newest kept.
    assert len(captured["frames"]) == 25
    assert captured["frames"] == chunks[-25:]


# ---------------------------------------------------------------------------
# Pre-wake VAD auto-calibration wiring
# ---------------------------------------------------------------------------


def test_wake_fire_uses_auto_vad_threshold_when_available(monkeypatch):
    """When the auto-calibrator produces a threshold, speech frames are
    classified against IT — not the static default. A 300-RMS chunk is
    speech under an auto threshold of 100 even though the static 500
    (provably dead on the prod mic) would have scored the window 0.00."""
    monkeypatch.setattr(
        wake_loop, "_auto_pre_wake_vad_threshold", lambda stats: 100.0,
    )
    sft = MagicMock(return_value={"text": "hi"})
    monkeypatch.setattr(wake_loop, "send_for_transcription", sft)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_distinct_chunk(300)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    assert sft.call_args.kwargs["pre_wake_speech_seconds"] == pytest.approx(
        0.08,
    )


def test_wake_fire_falls_back_to_static_vad_threshold(monkeypatch):
    """Auto-calibrator returning None (disabled / no stats) keeps the
    static threshold: the same 300-RMS chunk is below the fixture's
    static 500 and counts as ambient."""
    # Fixture default: _auto_pre_wake_vad_threshold → None, static 500.
    sft = MagicMock(return_value={"text": "hi"})
    monkeypatch.setattr(wake_loop, "send_for_transcription", sft)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_distinct_chunk(300)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    assert sft.call_args.kwargs["pre_wake_speech_seconds"] == 0.0


def test_wake_fire_reports_self_playback_false_by_default(monkeypatch):
    """Quiet-room fire: self_playback rides the send_for_transcription
    call as False (explicitly, so CC can distinguish "node says no" from
    "old node that doesn't know"), with no kind attached."""
    sft = MagicMock(return_value={"text": "hi"})
    monkeypatch.setattr(wake_loop, "send_for_transcription", sft)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    assert sft.call_args.kwargs["self_playback"] is False
    assert sft.call_args.kwargs["self_playback_kind"] is None


def test_self_playback_bypasses_auto_vad_calibration(monkeypatch):
    """During self-playback the pre-wake window is music bleed, so the
    auto VAD calibrator (which would set its "ambient floor" from the
    music) must be bypassed: the static threshold classifies the window,
    the raw value is STILL sent, and the payload carries the flag +
    kind so CC knows the number is unreliable."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    # Auto calibrator would say 100 → the 300-RMS chunk would count as
    # speech. Bypassed, the static 500 keeps it ambient → 0.0.
    auto = MagicMock(return_value=100.0)
    monkeypatch.setattr(wake_loop, "_auto_pre_wake_vad_threshold", auto)
    sft = MagicMock(return_value={"text": "hi"})
    monkeypatch.setattr(wake_loop, "send_for_transcription", sft)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_distinct_chunk(300)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    auto.assert_not_called()
    assert sft.call_args.kwargs["pre_wake_speech_seconds"] == 0.0
    assert sft.call_args.kwargs["self_playback"] is True
    assert sft.call_args.kwargs["self_playback_kind"] == "music"


def test_self_playback_bypasses_adaptive_silence_threshold(monkeypatch):
    """The adaptive silence threshold calibrates on the PRE-duck loud
    window, but the recording happens over ducked music — during
    self-playback it must be bypassed so listen() falls back to its
    static config default."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    adaptive = MagicMock(return_value=800)
    monkeypatch.setattr(wake_loop, "_adaptive_silence_threshold", adaptive)
    listen_mock = MagicMock(return_value="/tmp/cmd.wav")
    monkeypatch.setattr(wake_loop, "listen", listen_mock)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    adaptive.assert_not_called()
    assert listen_mock.call_args.kwargs["silence_threshold"] is None
    # The follow-up loop inherits the same static fallback.
    assert fu.call_args.kwargs["silence_threshold"] is None


def test_adaptive_silence_threshold_used_when_not_self_playing(monkeypatch):
    """Control for the bypass test: without self-playback the adaptive
    threshold flows to listen() exactly as before."""
    adaptive = MagicMock(return_value=800)
    monkeypatch.setattr(wake_loop, "_adaptive_silence_threshold", adaptive)
    listen_mock = MagicMock(return_value="/tmp/cmd.wav")
    monkeypatch.setattr(wake_loop, "listen", listen_mock)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    adaptive.assert_called_once()
    assert listen_mock.call_args.kwargs["silence_threshold"] == 800


def test_self_playback_fire_excluded_from_wake_calibration(monkeypatch):
    """A legitimate (non not_for_me) result during self-playback must
    NOT feed the wake-threshold auto-calibrator — music-time OWW scores
    are measured against a degraded signal and would poison the
    quiet-room sample set."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    monkeypatch.setattr(
        wake_loop, "send_for_transcription",
        lambda *a, **kw: {"text": "hello", "tool_calls": []},
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    record = MagicMock()
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", record)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    record.assert_not_called()


# ---------------------------------------------------------------------------
# Layer A telemetry — threshold_used / threshold_profile on 'Wake fired'
# ---------------------------------------------------------------------------


def _wake_fired_calls(logger_mock):
    """All structured 'Wake fired' logger.info calls."""
    return [
        c for c in logger_mock.info.call_args_list
        if c.args and c.args[0] == "Wake fired"
    ]


def _log_calls(logger_mock, message: str):
    return [
        c for c in logger_mock.info.call_args_list
        if c.args and c.args[0] == message
    ]


def test_wake_fired_log_tags_normal_threshold_profile(monkeypatch):
    """Quiet-room fire: 'Wake fired' carries threshold_used +
    threshold_profile='normal' and the Layer-B fields as False."""
    log = MagicMock()
    monkeypatch.setattr(wake_loop, "logger", log)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    calls = _wake_fired_calls(log)
    assert len(calls) == 1
    kw = calls[0].kwargs
    assert kw["threshold_used"] == pytest.approx(0.5)
    assert kw["threshold_profile"] == "normal"
    assert kw["tentative_triggered"] is False
    assert kw["tentative_completed"] is False


def test_wake_fired_log_tags_music_threshold_profile(monkeypatch):
    """Music-profile fire: the profile string and the effective (music)
    threshold ride the per-fire telemetry so Layer A can be peeled back
    with data."""
    monkeypatch.setattr(
        wake_loop, "current_wake_threshold_with_profile",
        lambda: (0.3, "music"),
    )
    log = MagicMock()
    monkeypatch.setattr(wake_loop, "logger", log)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.35])  # below normal 0.4/0.5, above music 0.3
    _run(bus, oww)

    calls = _wake_fired_calls(log)
    assert len(calls) == 1
    kw = calls[0].kwargs
    assert kw["threshold_used"] == pytest.approx(0.3)
    assert kw["threshold_profile"] == "music"


# ---------------------------------------------------------------------------
# Layer B — two-stage tentative wake (duck-assisted completion)
# ---------------------------------------------------------------------------


class NoDrainBus(FakeBus):
    """One chunk per blocking get: the drain-to-newest pass sees an
    empty queue, so every enqueued chunk is scored in sequence — the
    shape tentative-wake tests need (trigger chunk, then completion or
    expiry chunks)."""

    def subscribe(self, tag: str, maxsize: int | None = None):
        FakeBus.subscribe(self, tag, maxsize=maxsize)
        outer = self

        class _Q:
            def get(self, timeout=None):
                return outer._q.get(timeout=timeout)

            def get_nowait(self):
                raise queue.Empty

        return _Q()


def _tentative_config(monkeypatch, **overrides):
    """Config.get_float with tentative-layer overrides on top of prod
    defaults (tentative 0.20, window 1.6 s, cooldown 10 s)."""
    values = dict(overrides)
    monkeypatch.setattr(
        wake_loop.Config, "get_float",
        classmethod(lambda cls, key, default=0.0: values.get(key, default)),
    )


def test_tentative_duck_then_completion_fires_once(monkeypatch):
    """A tentative-band score during self-playback submits the duck
    (pause_active_playback) WITHOUT firing; when a subsequent score
    crosses the threshold inside the window, the normal fire path runs
    exactly once and 'Wake fired' carries tentative_triggered=True +
    tentative_completed=True."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    pause = MagicMock()
    monkeypatch.setattr(wake_loop, "pause_active_playback", pause)
    handle = MagicMock()
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", handle)
    log = MagicMock()
    monkeypatch.setattr(wake_loop, "logger", log)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = NoDrainBus(
        rate=16000, chunks=[_chunk_bytes(1280), _chunk_bytes(1280)],
    )
    oww = FakeOWW(scores=[0.25, 0.9])
    _run(bus, oww)

    # Duck submitted at tentative time AND by the normal fire path
    # (idempotent in music_control) — the tentative one came first.
    assert pause.call_count == 2
    handle.assert_called_once()
    assert len(_log_calls(log, "tentative wake triggered")) == 1
    calls = _wake_fired_calls(log)
    assert len(calls) == 1
    assert calls[0].kwargs["tentative_triggered"] is True
    assert calls[0].kwargs["tentative_completed"] is True
    # No expiry line — the window completed.
    assert _log_calls(log, "tentative wake expired") == []


def test_tentative_expiry_restores_music_and_plays_no_ack(monkeypatch):
    """A tentative that never completes must resume the music quietly:
    structured expiry line with score_peak + window, NO wake response
    (no ack), no fire, no calibration recording."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    _tentative_config(monkeypatch, tentative_wake_window_seconds=0.0)
    pause = MagicMock()
    resume = MagicMock()
    monkeypatch.setattr(wake_loop, "pause_active_playback", pause)
    monkeypatch.setattr(wake_loop, "resume_active_playback", resume)
    handle = MagicMock()
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", handle)
    record = MagicMock()
    monkeypatch.setattr(wake_loop, "record_legitimate_wake_score", record)
    log = MagicMock()
    monkeypatch.setattr(wake_loop, "logger", log)

    # Chunk 1 triggers the tentative (0.25 in [0.2, 0.5)); chunk 2's
    # low score arrives after the (zero-length) window → expiry.
    bus = NoDrainBus(
        rate=16000, chunks=[_chunk_bytes(1280), _chunk_bytes(1280)],
    )
    oww = FakeOWW(scores=[0.25, 0.05])
    _run(bus, oww)

    pause.assert_called_once()      # the tentative duck
    resume.assert_called_once()     # the quiet restore on expiry
    handle.assert_not_called()      # NO ack for a tentative
    record.assert_not_called()      # a tentative is NOT a fire
    expiries = _log_calls(log, "tentative wake expired")
    assert len(expiries) == 1
    assert expiries[0].kwargs["score_peak"] == pytest.approx(0.25)
    assert expiries[0].kwargs["window"] == 0.0
    assert _wake_fired_calls(log) == []


def test_tentative_cooldown_limits_to_one_per_window(monkeypatch):
    """Max one tentative per rolling cooldown: after a trigger+expiry,
    another tentative-band score inside the cooldown must NOT re-duck
    (lyric-induced dips can't strobe the music)."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    _tentative_config(monkeypatch, tentative_wake_window_seconds=0.0)
    pause = MagicMock()
    resume = MagicMock()
    monkeypatch.setattr(wake_loop, "pause_active_playback", pause)
    monkeypatch.setattr(wake_loop, "resume_active_playback", resume)

    # trigger → expire → in-band again (inside the 10 s cooldown) → quiet
    bus = NoDrainBus(
        rate=16000, chunks=[_chunk_bytes(1280) for _ in range(4)],
    )
    oww = FakeOWW(scores=[0.25, 0.05, 0.3, 0.05])
    _run(bus, oww)

    pause.assert_called_once()
    resume.assert_called_once()


def test_tentative_disabled_by_config(monkeypatch):
    """wake_word_tentative_threshold <= 0 disables the layer: an
    in-band score during self-playback never ducks."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    _tentative_config(monkeypatch, wake_word_tentative_threshold=0.0)
    pause = MagicMock()
    monkeypatch.setattr(wake_loop, "pause_active_playback", pause)

    bus = NoDrainBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.25])
    _run(bus, oww)

    pause.assert_not_called()


def test_tentative_requires_self_playback(monkeypatch):
    """Quiet room (is_self_playing False): tentative-band scores never
    trigger a duck — the layer exists only under self-playback."""
    pause = MagicMock()
    monkeypatch.setattr(wake_loop, "pause_active_playback", pause)

    bus = NoDrainBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.25])
    _run(bus, oww)

    pause.assert_not_called()


def test_debounce_suppressed_high_score_does_not_trigger_tentative(monkeypatch):
    """A score AT/ABOVE the wake threshold whose fire was suppressed by
    the debounce gate is not 'tentative band' — no duck. The tentative
    band is [tentative_threshold, wake_threshold) only."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)

    class _NoFire:
        should_fire = False

    monkeypatch.setattr(wake_loop, "decide_wake_fire", lambda **kw: _NoFire())
    pause = MagicMock()
    monkeypatch.setattr(wake_loop, "pause_active_playback", pause)

    bus = NoDrainBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    pause.assert_not_called()


def test_alert_break_expires_open_tentative(monkeypatch):
    """If the alert-check breaks the inner loop while a tentative window
    is open, the music must be resumed (not left ducked through the
    alert drain)."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    pause = MagicMock()
    resume = MagicMock()
    monkeypatch.setattr(wake_loop, "pause_active_playback", pause)
    monkeypatch.setattr(wake_loop, "resume_active_playback", resume)
    log = MagicMock()
    monkeypatch.setattr(wake_loop, "logger", log)

    # First iteration triggers the tentative; the second iteration's
    # alert check breaks the loop with the window still open.
    checks = {"n": 0}

    def _alerts_pending():
        checks["n"] += 1
        return checks["n"] >= 2

    monkeypatch.setattr(wake_loop, "_ALERT_CHECK_INTERVAL", 0)
    monkeypatch.setattr(
        wake_loop, "has_pending_high_priority_alerts", _alerts_pending,
    )
    drain = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "drain_alert_announcements", drain)

    bus = NoDrainBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.25])
    _run(bus, oww)

    pause.assert_called_once()
    resume.assert_called_once()
    expiries = _log_calls(log, "tentative wake expired")
    assert len(expiries) == 1
    assert expiries[0].kwargs["reason"] == "alert_drain_break"
    drain.assert_called_once()


def test_clip_deque_extended_by_tentative_window_during_music(monkeypatch):
    """During self-playback with the tentative layer enabled, the
    consumed-chunks clip deque covers WAKE_CLIP_SECONDS + the tentative
    window (2.0 + 1.6 = 3.6 s → 45 chunks at 80 ms) so the clip still
    contains the FULL phrase including the pre-duck chunks when a fire
    lands late in the window. Memory stays bounded (maxlen)."""
    monkeypatch.setattr(wake_loop, "is_self_playing", lambda: True)
    n = 50
    chunks = [_distinct_chunk(v + 1) for v in range(n)]
    captured: dict = {}

    def _capture(frames, bus):
        captured["frames"] = list(frames)
        return "/tmp/wake.wav"

    monkeypatch.setattr(
        wake_loop, "try_capture_wake_audio_from_frames", _capture,
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=chunks)
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    # (2.0 + 1.6) / 0.08 = 45 chunks, newest kept.
    assert len(captured["frames"]) == 45
    assert captured["frames"] == chunks[-45:]


def test_clip_deque_not_extended_in_quiet_room(monkeypatch):
    """Control: without self-playback the deque stays at the classic
    WAKE_CLIP_SECONDS sizing (25 chunks) even with the tentative layer
    enabled by default — no memory growth for the common case."""
    n = 30
    chunks = [_distinct_chunk(v + 1) for v in range(n)]
    captured: dict = {}

    def _capture(frames, bus):
        captured["frames"] = list(frames)
        return "/tmp/wake.wav"

    monkeypatch.setattr(
        wake_loop, "try_capture_wake_audio_from_frames", _capture,
    )
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=chunks)
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    assert len(captured["frames"]) == 25


def test_high_score_suppressed_then_alert_drains(monkeypatch):
    """Regression: a high OWW score that decide_wake_fire SUPPRESSES (e.g. the
    debounce gate trips while score > threshold) must not be mistaken for a
    wake when an alert then breaks the inner loop.

    The post-loop branch keys off whether a wake actually fired (`fired`), not
    the raw `score` — otherwise the stale-high score routes a suppressed
    detection into wake handling and the pending alert is silently dropped.
    """
    class _NoFire:
        should_fire = False

    # decide_wake_fire suppresses even a high score (models the debounce gate).
    monkeypatch.setattr(wake_loop, "decide_wake_fire", lambda **kw: _NoFire())
    # Alert fires on the SECOND inner iteration — after the high chunk has
    # scored — so `score` is left high (0.9) at the break.
    monkeypatch.setattr(wake_loop, "_ALERT_CHECK_INTERVAL", 2)
    monkeypatch.setattr(wake_loop, "has_pending_high_priority_alerts", lambda: True)

    drain = MagicMock(side_effect=KeyboardInterrupt())  # exit after the drain
    monkeypatch.setattr(wake_loop, "drain_alert_announcements", drain)
    # If the bug regressed, control would fall into wake handling instead —
    # make that path raise too so the test fails cleanly rather than hanging.
    handle = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", handle)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    drain.assert_called_once()   # alert drained despite the high suppressed score
    handle.assert_not_called()   # NOT treated as a wake


# ---------------------------------------------------------------------------
# Echo-cancel isolation — EC machinery must never take down the wake loop
# ---------------------------------------------------------------------------


def test_echo_cancel_watchdog_exception_never_propagates(monkeypatch):
    """A raising note_echo_cancel_chunk (the per-chunk EC watchdog feed)
    must not kill scoring or the fire chain — the wrapper in the loop
    swallows it and the wake still fires."""
    monkeypatch.setattr(
        wake_loop, "note_echo_cancel_chunk",
        MagicMock(side_effect=RuntimeError("ec machinery exploded")),
    )
    handle = MagicMock()
    fu = MagicMock(side_effect=KeyboardInterrupt())  # exit after one cycle
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", handle)
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    handle.assert_called_once()
    fu.assert_called_once()


def test_echo_cancel_telemetry_exception_never_blocks_fire(monkeypatch):
    """A raising echo_cancel_is_active at fire-telemetry time degrades to
    echo_cancel_active=False instead of aborting the fire."""
    monkeypatch.setattr(
        wake_loop, "echo_cancel_is_active",
        MagicMock(side_effect=RuntimeError("telemetry read failed")),
    )
    handle = MagicMock()
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "handle_keyword_detected", handle)
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    handle.assert_called_once()
    fu.assert_called_once()


def test_wake_fired_log_carries_echo_cancel_active(monkeypatch):
    """Per-fire telemetry: the 'Wake fired' structured log carries
    echo_cancel_active so the EC layer can be peeled back with data."""
    monkeypatch.setattr(wake_loop, "echo_cancel_is_active", lambda: True)
    log = MagicMock()
    monkeypatch.setattr(wake_loop, "logger", log)
    fu = MagicMock(side_effect=KeyboardInterrupt())
    monkeypatch.setattr(wake_loop, "follow_up_loop", fu)

    bus = FakeBus(rate=16000, chunks=[_chunk_bytes(1280)])
    oww = FakeOWW(scores=[0.9])
    _run(bus, oww)

    wake_fired = [
        c for c in log.info.call_args_list if c.args and c.args[0] == "Wake fired"
    ]
    assert len(wake_fired) == 1
    assert wake_fired[0].kwargs["echo_cancel_active"] is True
