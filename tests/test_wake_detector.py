"""Tests for the wake-detector module — threshold + fire-decision pipeline.

The wake-fire pipeline is the spine of every voice command. Two gates in
strict order:

  1. **Score gate** — oww.score must clear the configured static /
     auto-calibrated threshold. There is no separate music threshold:
     the static threshold (~0.40) already rejects speaker bleed (which
     scores ~0.10–0.18) while genuine wakes score above it, so playback
     no longer needs a lowered threshold or an energy gate to compensate.

  2. **Debounce gate** — atomic under ``voice_filters._wake_gate_lock``,
     check if ``_wake_min_next_ts`` is in the future. If yes, suppress
     (probably the same utterance double-firing). If no, advance the
     gate by ``_WAKE_DEBOUNCE_SEC`` and let the fire through.

Coverage focus:

  * ``current_wake_threshold`` — DB-backed Config wrapper around the
    static-default / auto-calibrated branches.

  * ``locked_oww_reset`` — single ``oww.reset()`` under the shared lock
    so it serialises with any in-flight ``oww.predict()`` in the
    barge-in monitor.

  * ``decide_wake_fire`` — the whole pipeline as one verdict. Each gate
    is exercised in isolation by feeding inputs that pass earlier gates
    and trip exactly the gate under test.

The module mutates ``voice_filters._wake_min_next_ts`` under the lock
on a fired verdict; tests reset that state per-case via an autouse
fixture.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from core import voice_filters
from core import wake_detector


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_wake_gate():
    """Reset the wake-debounce gate to 0 before each test so cooldown
    state doesn't leak between cases."""
    voice_filters.reset_wake_gate()
    yield
    voice_filters.reset_wake_gate()


def _stable_config(monkeypatch, **overrides):
    """Install a deterministic Config so the test result depends on the
    inputs to current_wake_threshold, not on environmental DB drift."""
    floats = {"wake_word_threshold": 0.4}
    floats.update({k: v for k, v in overrides.items() if isinstance(v, float)})
    bools = {"wake_word_threshold_auto": False}
    bools.update({k: v for k, v in overrides.items() if isinstance(v, bool)})

    monkeypatch.setattr(
        wake_detector.Config, "get_float",
        lambda key, default: floats.get(key, default),
    )
    monkeypatch.setattr(
        wake_detector.Config, "get_bool",
        lambda key, default: bools.get(key, default),
    )


# ---------------------------------------------------------------------------
# current_wake_threshold — DB-backed selection between static / auto
# ---------------------------------------------------------------------------


class TestCurrentWakeThreshold:

    def test_returns_static_default(self, monkeypatch):
        _stable_config(monkeypatch)
        assert wake_detector.current_wake_threshold() == 0.4

    def test_auto_enabled_uses_calibrator(self, monkeypatch):
        _stable_config(monkeypatch, wake_word_threshold_auto=True)
        # Calibrator gets the static default as its fallback; return a
        # distinct value so we can assert it's the path taken.
        monkeypatch.setattr(
            wake_detector, "auto_calibrated_wake_threshold",
            lambda fallback: 0.27,
        )
        assert wake_detector.current_wake_threshold() == 0.27

    def test_auto_disabled_does_not_consult_calibrator(self, monkeypatch):
        _stable_config(monkeypatch, wake_word_threshold_auto=False)
        called: list[float] = []
        monkeypatch.setattr(
            wake_detector, "auto_calibrated_wake_threshold",
            lambda fb: called.append(fb) or 0.27,
        )
        assert wake_detector.current_wake_threshold() == 0.4
        assert called == []  # calibrator never consulted


# ---------------------------------------------------------------------------
# locked_oww_reset — single reset under the shared lock
# ---------------------------------------------------------------------------


class TestLockedOwwReset:

    def test_calls_reset(self, monkeypatch):
        oww = MagicMock()
        wake_detector.locked_oww_reset(oww)
        oww.reset.assert_called_once()

    def test_holds_lock_around_reset(self, monkeypatch):
        # If something else holds _oww_lock when we call reset(), our
        # function must block until it's released. We verify this by
        # making reset() observe the lock state.
        observed = []

        def observe_lock_state():
            observed.append(wake_detector._oww_lock.locked())

        oww = MagicMock()
        oww.reset.side_effect = observe_lock_state
        wake_detector.locked_oww_reset(oww)
        # The lock was held when reset ran.
        assert observed == [True]
        # And it's released by the time the function returns.
        assert wake_detector._oww_lock.locked() is False


# ---------------------------------------------------------------------------
# decide_wake_fire — score gate then debounce gate
# ---------------------------------------------------------------------------


class TestDecideWakeFireScoreGate:
    """First gate: score must clear the threshold."""

    def test_below_threshold_no_fire(self):
        verdict = wake_detector.decide_wake_fire(
            score=0.30, threshold=0.4, now_mono=100.0,
        )
        assert verdict.should_fire is False
        assert verdict.effective_threshold == 0.4

    def test_above_threshold_fires(self):
        verdict = wake_detector.decide_wake_fire(
            score=0.45, threshold=0.4, now_mono=100.0,
        )
        assert verdict.should_fire is True
        assert verdict.effective_threshold == 0.4
        # Gate advanced 8 seconds into the future.
        assert voice_filters.get_wake_min_next_ts() == pytest.approx(108.0)

    def test_below_threshold_does_not_touch_gate(self):
        # Score gate fails → the debounce cooldown must not be advanced
        # (no fire = no debounce).
        voice_filters._wake_min_next_ts = 50.0
        verdict = wake_detector.decide_wake_fire(
            score=0.10, threshold=0.4, now_mono=100.0,
        )
        assert verdict.should_fire is False
        assert voice_filters.get_wake_min_next_ts() == 50.0


class TestDecideWakeFireDebounceGate:
    """Second gate: same-utterance debounce. The cooldown advances when a
    fire passes through, and is checked atomically under the lock."""

    def test_cooldown_active_suppresses(self):
        # Force the gate to be in the future relative to now_mono.
        voice_filters._wake_min_next_ts = 200.0
        verdict = wake_detector.decide_wake_fire(
            score=0.45, threshold=0.4, now_mono=100.0,
        )
        assert verdict.should_fire is False
        # Gate is unchanged — a suppressed fire MUST NOT extend the
        # cooldown (would create unbounded extension under repeated
        # noise hits).
        assert voice_filters.get_wake_min_next_ts() == 200.0

    def test_cooldown_elapsed_advances_gate(self):
        voice_filters._wake_min_next_ts = 50.0  # in the past
        verdict = wake_detector.decide_wake_fire(
            score=0.45, threshold=0.4, now_mono=100.0,
        )
        assert verdict.should_fire is True
        # Advanced by _WAKE_DEBOUNCE_SEC (8.0).
        assert voice_filters.get_wake_min_next_ts() == pytest.approx(108.0)

    def test_gate_advancement_uses_now_not_old_ts(self):
        # If the gate is already in the past, the new value is
        # now + debounce — not old_ts + debounce. Verifies we don't
        # accumulate stale time.
        voice_filters._wake_min_next_ts = 0.0
        verdict = wake_detector.decide_wake_fire(
            score=0.45, threshold=0.4, now_mono=12345.0,
        )
        assert verdict.should_fire is True
        assert voice_filters.get_wake_min_next_ts() == pytest.approx(12353.0)


# ---------------------------------------------------------------------------
# WakeVerdict — small frozen dataclass; checks fields exist as expected
# ---------------------------------------------------------------------------


class TestWakeVerdict:

    def test_verdict_carries_decision_metadata(self):
        v = wake_detector.decide_wake_fire(
            score=0.45, threshold=0.4, now_mono=100.0,
        )
        assert hasattr(v, "should_fire")
        assert hasattr(v, "effective_threshold")
