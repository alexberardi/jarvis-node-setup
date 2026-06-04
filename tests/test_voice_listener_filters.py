"""Tests for the transcript filter + wake-suppression policy used by
``scripts/voice_listener.py``.

Imports from ``core.voice_filters`` directly so the test runs in any
environment with the project on ``PYTHONPATH`` (no audio drivers, no
sqlcipher, no libspeexdsp). voice_listener re-exports the same names
at runtime.
"""

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
# Wake-acceptance gate — debounce only
# ---------------------------------------------------------------------------
#
# The not_for_me cool-down + escalation mechanism that used to live here was
# removed: locking the user out for tens of seconds after a probabilistic
# verdict is the wrong abstraction. Misclassifies now silently skip TTS and
# the next wake is accepted immediately. Voice_listener still pushes the
# gate forward by ``_WAKE_DEBOUNCE_SEC`` on every accepted wake to swallow
# openWakeWord's same-utterance double-fire — that policy is asserted at
# the wake-fire site, not here.


@pytest.fixture(autouse=True)
def _silence_module_logs(monkeypatch):
    """voice_filters logs to the shared JarvisLogger which falls back to
    console when the remote logs endpoint is unavailable. Silence the
    fallback so test output stays readable."""
    monkeypatch.setattr(
        voice_filters.logger, "info", lambda *_a, **_kw: None
    )
