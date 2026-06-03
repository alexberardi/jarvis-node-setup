"""Tests for the transcript filter + wake-suppression policy used by
``scripts/voice_listener.py``.

Imports from ``core.voice_filters`` directly so the test runs in any
environment with the project on ``PYTHONPATH`` (no audio drivers, no
sqlcipher, no libspeexdsp). voice_listener re-exports the same names
at runtime.
"""

import time

import pytest

from core import voice_filters


# ---------------------------------------------------------------------------
# Transcript filter
# ---------------------------------------------------------------------------


class TestIsNonSpeechBracketed:
    """Reject bracketed Whisper annotations regardless of bracket flavor."""

    def test_asterisk_sniff(self):
        # The 2026-06-02 prod regression input — must be silenced at
        # the node so it never reaches CC at all.
        assert voice_filters.is_non_speech("*sniff*")

    def test_asterisk_sad_noises(self):
        # Whitespace inside the asterisk pair must still match.
        assert voice_filters.is_non_speech("*sad noises*")

    def test_asterisk_with_trailing_period(self):
        # Whisper sometimes appends sentence-final punctuation.
        assert voice_filters.is_non_speech("*sniff*.")
        assert voice_filters.is_non_speech("*sniff*,")
        assert voice_filters.is_non_speech("*sniff*!")

    def test_square_bracket(self):
        assert voice_filters.is_non_speech("[BLANK_AUDIO]")
        assert voice_filters.is_non_speech("[music]")

    def test_paren_annotation(self):
        assert voice_filters.is_non_speech("(silence)")
        assert voice_filters.is_non_speech("(wind blowing)")

    def test_angle_bracket(self):
        assert voice_filters.is_non_speech("<inaudible>")

    def test_consecutive_bracketed_tokens(self):
        assert voice_filters.is_non_speech("*sniff* *cough*")
        assert voice_filters.is_non_speech("[laughter] (sigh)")

    def test_whitespace_around_bracket(self):
        assert voice_filters.is_non_speech("  *sniff*  ")


class TestIsNonSpeechRealUtterances:
    """Real speech must NEVER trip the filter — that was the v1 bug the
    big docstring warns against."""

    def test_short_imperative(self):
        assert not voice_filters.is_non_speech("Stop.")

    def test_question(self):
        assert not voice_filters.is_non_speech("what's the weather")

    def test_filler_words(self):
        # Single-word fillers must still pass through to CC's LLM check.
        assert not voice_filters.is_non_speech("okay")
        assert not voice_filters.is_non_speech("yeah")
        assert not voice_filters.is_non_speech("bye")

    def test_brackets_mid_sentence(self):
        # A real utterance that happens to contain brackets mid-string is
        # NOT bracketed-only and must reach CC.
        assert not voice_filters.is_non_speech("open the *kitchen* light")

    def test_empty_returns_true(self):
        assert voice_filters.is_non_speech("")
        assert voice_filters.is_non_speech(None)
        assert voice_filters.is_non_speech("   ")


# ---------------------------------------------------------------------------
# Wake-suppression gate
# ---------------------------------------------------------------------------


class TestSuppressWakeFor:
    """The wake-acceptance gate is monotonically pushed forward only."""

    def setup_method(self):
        voice_filters.reset_wake_gate()

    def test_extends_when_target_is_further(self):
        voice_filters.suppress_wake_for(10.0, reason="test")
        first = voice_filters.get_wake_min_next_ts()
        # A 20s push is further out — must replace the gate.
        voice_filters.suppress_wake_for(20.0, reason="test")
        assert voice_filters.get_wake_min_next_ts() > first

    def test_does_not_shrink_gate(self):
        voice_filters.suppress_wake_for(60.0, reason="long")
        long_target = voice_filters.get_wake_min_next_ts()
        # A 5s push would shrink the gate — must be a no-op.
        voice_filters.suppress_wake_for(5.0, reason="short")
        assert voice_filters.get_wake_min_next_ts() == long_target

    def test_zero_or_negative_seconds_is_noop(self):
        voice_filters.suppress_wake_for(0.0, reason="zero")
        assert voice_filters.get_wake_min_next_ts() == 0.0
        voice_filters.suppress_wake_for(-5.0, reason="neg")
        assert voice_filters.get_wake_min_next_ts() == 0.0


class TestNotForMeMultiFireCooldown:
    """A single ``not_for_me`` uses the standard cool-down; ≥2 within the
    rolling window escalate to the longer cool-down. Side conversations
    cluster — once we've seen two we widen the gate before more wakes
    fire on the same conversation.
    """

    def setup_method(self):
        voice_filters.reset_wake_gate()

    def test_single_event_returns_standard_cooldown(self, monkeypatch):
        def fake_get_float(key, default):
            return default
        monkeypatch.setattr(voice_filters.Config, "get_float", fake_get_float)

        cooldown = voice_filters.record_not_for_me_event()
        assert cooldown == voice_filters._NOT_FOR_ME_QUIET_SEC_DEFAULT

    def test_second_event_within_window_escalates(self, monkeypatch):
        def fake_get_float(key, default):
            return default
        monkeypatch.setattr(voice_filters.Config, "get_float", fake_get_float)

        voice_filters.record_not_for_me_event()
        cooldown = voice_filters.record_not_for_me_event()
        assert cooldown == voice_filters._NOT_FOR_ME_ESCALATED_SEC_DEFAULT

    def test_second_event_outside_window_does_not_escalate(self, monkeypatch):
        def fake_get_float(key, default):
            return default
        monkeypatch.setattr(voice_filters.Config, "get_float", fake_get_float)

        # First fire lands far in the past; the prune step in the
        # tracker should drop it so the next call sees count=1.
        old_ts = time.monotonic() - voice_filters._NOT_FOR_ME_HISTORY_WINDOW_SEC - 5.0
        voice_filters._not_for_me_history.append(old_ts)

        cooldown = voice_filters.record_not_for_me_event()
        assert cooldown == voice_filters._NOT_FOR_ME_QUIET_SEC_DEFAULT
        # The stale entry must have been pruned, not retained.
        assert all(
            t >= time.monotonic() - voice_filters._NOT_FOR_ME_HISTORY_WINDOW_SEC
            for t in voice_filters._not_for_me_history
        )

    def test_escalated_cooldown_is_configurable(self, monkeypatch):
        # Verify the Config key is consulted on the escalated path so
        # ops can tune without code changes.
        seen_keys: list[str] = []

        def fake_get_float(key, default):
            seen_keys.append(key)
            return default
        monkeypatch.setattr(voice_filters.Config, "get_float", fake_get_float)

        voice_filters.record_not_for_me_event()
        voice_filters.record_not_for_me_event()
        assert "not_for_me_escalated_quiet_seconds" in seen_keys


@pytest.fixture(autouse=True)
def _silence_module_logs(monkeypatch):
    """voice_filters logs to the shared JarvisLogger which falls back to
    console when the remote logs endpoint is unavailable. Silence the
    fallback so test output stays readable."""
    monkeypatch.setattr(
        voice_filters.logger, "info", lambda *_a, **_kw: None
    )
