"""Tests for the wake-detector module — threshold + fire-decision pipeline.

The wake-fire pipeline is the spine of every voice command. Three gates
in strict order:

  1. **Score gate** — oww.score must clear ``effective_threshold``,
     where ``effective_threshold`` is the music threshold when playback
     is detected (lower; ~0.12) and the configured static / auto-
     calibrated threshold otherwise (~0.40).

  2. **Music-bleed gate** — when music is playing, raw RMS must spike
     above a running baseline by ``music_energy_multiplier``. This
     prevents the lowered music threshold from tripping on speaker bleed
     alone. Two exemptions: (a) AEC is strongly cancelling (the post-AEC
     score is already trustworthy), (b) the score is above the trust
     threshold (default 0.95) — at that confidence we defer to the
     model. With insufficient RMS history (<6 samples) we conservatively
     don't fire on the music threshold at all.

  3. **Debounce gate** — atomic under ``voice_filters._wake_gate_lock``,
     check if ``_wake_min_next_ts`` is in the future. If yes, suppress
     (probably the same utterance double-firing). If no, advance the
     gate by ``_WAKE_DEBOUNCE_SEC`` and let the fire through.

Coverage focus:

  * ``current_wake_threshold`` — DB-backed Config wrapper around
    music-mode / auto-calibrated / static-default branches.

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

from collections import deque
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


def _aec(*, recent_ref: bool = False, strong: bool = False,
         suppression_db: float = 0.0):
    """Build a MagicMock aec_pipeline with the methods decide_wake_fire calls."""
    m = MagicMock()
    m.has_recent_reference_signal = MagicMock(return_value=recent_ref)
    m.is_strongly_active = MagicMock(return_value=strong)
    m.recent_suppression_db = MagicMock(return_value=suppression_db)
    return m


def _stable_config(monkeypatch, **overrides):
    """Install a deterministic Config so the test result depends on the
    inputs to decide_wake_fire, not on environmental DB drift."""
    floats = {
        "wake_word_threshold_music": 0.12,
        "wake_word_threshold": 0.4,
        "aec_trust_threshold_db": 5.0,
        "wake_music_trust_score": 0.95,
    }
    floats.update({k: v for k, v in overrides.items() if isinstance(v, float)})
    bools = {
        "wake_word_threshold_auto": False,
    }
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
# current_wake_threshold — DB-backed selection between music / static / auto
# ---------------------------------------------------------------------------


class TestCurrentWakeThreshold:

    def test_music_mode_uses_music_threshold(self, monkeypatch):
        # Music playing → return the music-mode threshold regardless of
        # auto-calibration flag.
        _stable_config(monkeypatch)
        monkeypatch.setattr(wake_detector, "music_is_playing", lambda: True)
        assert wake_detector.current_wake_threshold() == 0.12

    def test_no_music_returns_static_default(self, monkeypatch):
        _stable_config(monkeypatch)
        monkeypatch.setattr(wake_detector, "music_is_playing", lambda: False)
        assert wake_detector.current_wake_threshold() == 0.4

    def test_no_music_auto_enabled_uses_calibrator(self, monkeypatch):
        _stable_config(monkeypatch, wake_word_threshold_auto=True)
        monkeypatch.setattr(wake_detector, "music_is_playing", lambda: False)
        # Calibrator gets the static default as its fallback; return a
        # distinct value so we can assert it's the path taken.
        monkeypatch.setattr(
            wake_detector, "auto_calibrated_wake_threshold",
            lambda fallback: 0.27,
        )
        assert wake_detector.current_wake_threshold() == 0.27

    def test_music_mode_skips_auto_calibration(self, monkeypatch):
        # Music branch returns BEFORE the auto check — so auto=True with
        # music playing should still use the music threshold, not the
        # calibrator.
        _stable_config(monkeypatch, wake_word_threshold_auto=True)
        monkeypatch.setattr(wake_detector, "music_is_playing", lambda: True)
        called: list[float] = []
        monkeypatch.setattr(
            wake_detector, "auto_calibrated_wake_threshold",
            lambda fb: called.append(fb) or 0.27,
        )
        assert wake_detector.current_wake_threshold() == 0.12
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
# decide_wake_fire — the full three-gate pipeline
# ---------------------------------------------------------------------------


class TestDecideWakeFireScoreGate:
    """First gate: score must clear the effective threshold."""

    def test_below_threshold_no_fire(self, monkeypatch):
        _stable_config(monkeypatch)
        verdict = wake_detector.decide_wake_fire(
            score=0.30,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=False,
            aec_pipeline=None,
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is False
        assert verdict.effective_threshold == 0.4
        assert verdict.effective_music_mode is False

    def test_above_threshold_fires(self, monkeypatch):
        _stable_config(monkeypatch)
        verdict = wake_detector.decide_wake_fire(
            score=0.45,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=False,
            aec_pipeline=None,
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is True
        assert verdict.effective_threshold == 0.4
        assert verdict.effective_music_mode is False
        # Gate advanced 8 seconds into the future.
        assert voice_filters.get_wake_min_next_ts() == pytest.approx(108.0)

    def test_music_mode_uses_lower_music_threshold(self, monkeypatch):
        # 0.20 wouldn't fire against the static 0.4, but the music
        # threshold is 0.12 — so it does. Pass the music-bleed gate by
        # giving a strong AEC.
        _stable_config(monkeypatch)
        verdict = wake_detector.decide_wake_fire(
            score=0.20,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=True,
            aec_pipeline=_aec(strong=True, suppression_db=10.0),
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is True
        assert verdict.effective_threshold == 0.12
        assert verdict.effective_music_mode is True


class TestDecideWakeFireMusicBleedGate:
    """Second gate: when music_mode and AEC isn't trusted, require the
    RMS to spike above the running baseline."""

    def test_aec_trusted_skips_energy_gate(self, monkeypatch):
        # Strongly active AEC → energy gate is bypassed even with no
        # RMS history. Critical for the case where AEC genuinely is
        # cancelling music well; the gate would otherwise add a failure
        # mode.
        _stable_config(monkeypatch)
        verdict = wake_detector.decide_wake_fire(
            score=0.50,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=True,
            aec_pipeline=_aec(strong=True, suppression_db=8.0),
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is True

    def test_energy_floor_suppresses_music_bleed(self, monkeypatch):
        # No AEC, sufficient RMS history (>= 6), but the current RMS is
        # at the baseline (music-only, no voice on top). Must suppress.
        _stable_config(monkeypatch)
        baseline_rms = 100.0
        verdict = wake_detector.decide_wake_fire(
            score=0.20,
            rms=baseline_rms,
            # All-equal history → median == 100 → floor == 150 (×1.5).
            pre_wake_rms_values=deque([baseline_rms] * 6),
            music_mode=True,
            aec_pipeline=_aec(strong=False),
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is False

    def test_energy_spike_above_floor_fires(self, monkeypatch):
        # Voice on top of music — RMS spikes above the baseline*multiplier.
        _stable_config(monkeypatch)
        baseline_rms = 100.0
        verdict = wake_detector.decide_wake_fire(
            score=0.20,
            rms=200.0,  # well above 1.5 × 100
            pre_wake_rms_values=deque([baseline_rms] * 6),
            music_mode=True,
            aec_pipeline=_aec(strong=False),
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is True

    def test_trust_score_bypasses_energy_gate(self, monkeypatch):
        # Score above wake_music_trust_score (0.95) → bypass the energy
        # gate even when RMS would have suppressed. This is the "user
        # yelling Hey Jarvis to stop their own music" case — the model
        # is essentially certain, defer to it.
        _stable_config(monkeypatch)
        baseline_rms = 100.0
        verdict = wake_detector.decide_wake_fire(
            score=0.97,
            rms=baseline_rms,  # at baseline → would suppress without trust
            pre_wake_rms_values=deque([baseline_rms] * 6),
            music_mode=True,
            aec_pipeline=_aec(strong=False),
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is True

    def test_insufficient_baseline_suppresses(self, monkeypatch):
        # Music mode + no AEC + fewer than 6 RMS samples → conservative
        # no-fire. Without enough baseline we can't tell music-only
        # from voice-on-music.
        _stable_config(monkeypatch)
        verdict = wake_detector.decide_wake_fire(
            score=0.20,
            rms=500.0,  # huge, but baseline is unknown
            pre_wake_rms_values=deque([100.0] * 3),
            music_mode=True,
            aec_pipeline=_aec(strong=False),
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is False

    def test_effective_music_mode_inferred_from_aec(self, monkeypatch):
        # music_mode=False at outer-loop entry, but AEC has detected
        # reference signal in the last chunk → music is playing NOW.
        # Treat as music_mode and use the lower threshold.
        _stable_config(monkeypatch)
        verdict = wake_detector.decide_wake_fire(
            score=0.20,
            rms=200.0,
            pre_wake_rms_values=deque([100.0] * 6),
            music_mode=False,
            aec_pipeline=_aec(recent_ref=True, strong=True, suppression_db=10.0),
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.effective_music_mode is True
        assert verdict.effective_threshold == 0.12
        assert verdict.should_fire is True


class TestDecideWakeFireDebounceGate:
    """Third gate: same-utterance debounce. The cooldown advances when
    a fire passes through, and is checked atomically under the lock."""

    def test_cooldown_active_suppresses(self, monkeypatch):
        _stable_config(monkeypatch)
        # Force the gate to be in the future relative to now_mono.
        voice_filters._wake_min_next_ts = 200.0
        verdict = wake_detector.decide_wake_fire(
            score=0.45,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=False,
            aec_pipeline=None,
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is False
        # Gate is unchanged — a suppressed fire MUST NOT extend the
        # cooldown (would create unbounded extension under repeated
        # noise hits).
        assert voice_filters.get_wake_min_next_ts() == 200.0

    def test_cooldown_elapsed_advances_gate(self, monkeypatch):
        _stable_config(monkeypatch)
        voice_filters._wake_min_next_ts = 50.0  # in the past
        verdict = wake_detector.decide_wake_fire(
            score=0.45,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=False,
            aec_pipeline=None,
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is True
        # Advanced by _WAKE_DEBOUNCE_SEC (8.0).
        assert voice_filters.get_wake_min_next_ts() == pytest.approx(108.0)

    def test_gate_advancement_uses_now_not_old_ts(self, monkeypatch):
        # If the gate is already in the past, the new value is
        # now + debounce — not old_ts + debounce. Verifies we don't
        # accumulate stale time.
        _stable_config(monkeypatch)
        voice_filters._wake_min_next_ts = 0.0
        verdict = wake_detector.decide_wake_fire(
            score=0.45,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=False,
            aec_pipeline=None,
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=12345.0,
        )
        assert verdict.should_fire is True
        assert voice_filters.get_wake_min_next_ts() == pytest.approx(12353.0)

    def test_below_threshold_does_not_touch_gate(self, monkeypatch):
        # Score gate fails BEFORE the cooldown gate — the cooldown
        # value must not be advanced (no fire = no debounce).
        _stable_config(monkeypatch)
        voice_filters._wake_min_next_ts = 50.0
        verdict = wake_detector.decide_wake_fire(
            score=0.10,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=False,
            aec_pipeline=None,
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        assert verdict.should_fire is False
        assert voice_filters.get_wake_min_next_ts() == 50.0


# ---------------------------------------------------------------------------
# WakeVerdict — small frozen dataclass; checks fields exist as expected
# ---------------------------------------------------------------------------


class TestWakeVerdict:

    def test_verdict_carries_decision_metadata(self, monkeypatch):
        _stable_config(monkeypatch)
        v = wake_detector.decide_wake_fire(
            score=0.45,
            rms=0.0,
            pre_wake_rms_values=deque(),
            music_mode=False,
            aec_pipeline=None,
            static_wake_threshold=0.4,
            music_energy_multiplier=1.5,
            now_mono=100.0,
        )
        # All public fields the caller needs to dispatch on are present.
        assert hasattr(v, "should_fire")
        assert hasattr(v, "effective_threshold")
        assert hasattr(v, "effective_music_mode")
