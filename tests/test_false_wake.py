"""Tests for the false-wake detector — characterizes the policy that
turns ambient room speech into a silent abort instead of a CC round-trip.

Test corpus is drawn from real prod kitchen incidents plus the existing
``_ABORT_PHRASES`` set, so regressions in any of the four detection
signals fail loud:

  1. Abort phrases ("cancel", "nevermind", ...)
  2. Recording hit max + long-ish transcription = ambient
  3. Multi-sentence past word threshold = narration shape
  4. Whisper segment-timing shape (long run-on or gap-less multi-segment)

Direct "jarvis" addressing is the override that beats signals 3 + 4 —
real multi-sentence commands like "jarvis, set a timer. ten minutes."
must NOT be flagged.
"""

import pytest

from core.false_wake import (
    count_sentences,
    is_false_wake,
    looks_like_narration_by_segments,
)
from scripts.speech_to_text import RecordingResult


def _rec(duration: float = 1.5, hit_max: bool = False) -> RecordingResult:
    return RecordingResult(
        audio_file="",
        duration=duration,
        hit_max_duration=hit_max,
    )


# ---------------------------------------------------------------------------
# Sentence counting — Whisper punctuation is reliable but we still need to
# treat consecutive terminal marks ("?!", "...") as one boundary.
# ---------------------------------------------------------------------------


class TestCountSentences:
    @pytest.mark.parametrize("text,expected", [
        ("Turn on the kitchen lights",                  0),  # no terminal
        ("Turn on the kitchen lights.",                 1),
        ("Turn on the lights. Set a timer.",            2),
        ("Wait... what was that?",                      2),  # ... counts once
        ("Really?! Like really?!",                      2),  # ?! counts once
        ("",                                            0),
    ])
    def test_counts(self, text, expected):
        assert count_sentences(text) == expected


# ---------------------------------------------------------------------------
# Whisper segment-shape narration detector — bypasses LLM round-trip when
# the audio is one long continuous run-on or a series of gap-less segments.
# ---------------------------------------------------------------------------


class TestLooksLikeNarrationBySegments:
    def test_under_word_threshold_never_narration(self):
        # Short utterances are never narration even with one long segment.
        segs = [{"t0_ms": 0, "t1_ms": 5000}]
        assert looks_like_narration_by_segments(5, segs) is False

    def test_no_segments_returns_false(self):
        # Older whisper-api builds don't expose timing — we don't penalize.
        assert looks_like_narration_by_segments(20, None) is False
        assert looks_like_narration_by_segments(20, []) is False

    def test_single_long_segment_is_narration(self):
        segs = [{"t0_ms": 0, "t1_ms": 4500}]
        assert looks_like_narration_by_segments(20, segs) is True

    def test_single_short_segment_not_narration(self):
        segs = [{"t0_ms": 0, "t1_ms": 3500}]  # under 4s
        assert looks_like_narration_by_segments(20, segs) is False

    def test_gapless_multi_segment_is_narration(self):
        # Multiple segments with all gaps under 300 ms = stitched narration
        segs = [
            {"t0_ms": 0,    "t1_ms": 1200},
            {"t0_ms": 1300, "t1_ms": 2400},   # 100 ms gap
            {"t0_ms": 2600, "t1_ms": 3500},   # 200 ms gap
        ]
        assert looks_like_narration_by_segments(20, segs) is True

    def test_multi_segment_with_real_pause_not_narration(self):
        # A real >300 ms inter-thought pause = command shape, not ambient
        segs = [
            {"t0_ms": 0,    "t1_ms": 1200},
            {"t0_ms": 1800, "t1_ms": 2400},   # 600 ms gap = real pause
        ]
        assert looks_like_narration_by_segments(20, segs) is False


# ---------------------------------------------------------------------------
# Full is_false_wake policy — corpus drawn from prod kitchen incidents
# and the abort-phrases set. Reads top-to-bottom: signal 1, then 2, then
# the "jarvis" override, then signals 3 + 4.
# ---------------------------------------------------------------------------


class TestIsFalseWakeAbortPhrases:
    """Signal 1 — explicit abort phrases short-circuit before any shape check."""

    @pytest.mark.parametrize("phrase", [
        "cancel",
        "nevermind",
        "never mind",
        "forget it",
        "not you",
        "sorry jarvis",
        "ignore that",
        "ignore me",
        "that wasn't for you",
    ])
    def test_each_abort_phrase(self, phrase):
        assert is_false_wake(phrase, _rec()) is True

    def test_abort_phrase_as_prefix(self):
        # Matches startswith — user trails off after the abort.
        assert is_false_wake("cancel that please", _rec()) is True

    def test_abort_phrase_is_case_insensitive(self):
        assert is_false_wake("Cancel", _rec()) is True
        assert is_false_wake("NEVERMIND", _rec()) is True


class TestIsFalseWakeHitMax:
    """Signal 2 — recording stopped only because the user kept talking."""

    def test_long_transcript_at_max_is_ambient(self):
        # >20 words + hit max = continuous ambient conversation
        text = (
            "yeah and then she said well I don't think that's a good idea "
            "so we agreed to wait until next week"
        )
        assert is_false_wake(text, _rec(duration=7.0, hit_max=True)) is True

    def test_short_transcript_at_max_starts_lowercase_is_ambient(self):
        # Mid-sentence start (lowercase, not 'i ' / 'i'' / 'ok') = overheard
        assert is_false_wake(
            "and that's when she said it would be fine",
            _rec(duration=7.0, hit_max=True),
        ) is True

    def test_hit_max_starting_with_capital_I_is_allowed(self):
        # Starts with "I " — real first-person command, even at max duration.
        assert is_false_wake(
            "I want to know what time it is",
            _rec(duration=7.0, hit_max=True),
        ) is False

    def test_hit_max_starting_with_lowercase_i_apostrophe_is_allowed(self):
        # "i'm" / "i'd" lowercase exemption matches the production rule.
        assert is_false_wake(
            "i'm not sure what you mean by that",
            _rec(duration=7.0, hit_max=True),
        ) is False

    def test_hit_max_starting_with_ok_is_allowed(self):
        assert is_false_wake(
            "ok let's go ahead and set that timer for me",
            _rec(duration=7.0, hit_max=True),
        ) is False


class TestIsFalseWakeJarvisOverride:
    """The "jarvis" addressing check beats every shape signal."""

    def test_multi_sentence_with_jarvis_passes(self):
        # The exact comment-cited example from voice_listener.
        text = "jarvis, set a timer. ten minutes."
        assert is_false_wake(text, _rec(duration=2.5)) is False

    def test_jarvis_with_segment_narration_shape_passes(self):
        # Even when segment timing looks like narration, "jarvis" wins.
        text = (
            "jarvis turn on the lights and lock the doors and set the "
            "thermostat to seventy"
        )
        segs = [{"t0_ms": 0, "t1_ms": 5000}]  # would otherwise flag narration
        assert is_false_wake(text, _rec(duration=5.0), segments=segs) is False


class TestIsFalseWakeShape:
    """Signals 3 + 4 — narration shape (multi-sentence past threshold,
    or whisper segment-shape continuous run-on)."""

    def test_real_prod_false_wake_2026_06_03(self):
        # The actual transcript captured by prod kitchen at 18:41:07 —
        # ambient TV / family conversation that triggered a false wake.
        # Multi-sentence + word count past threshold.
        text = "I'm playing the outside of it. I know a con."
        assert is_false_wake(text, _rec(duration=2.4)) is True

    def test_single_sentence_under_threshold_passes(self):
        # Short single-sentence command — the happy path.
        assert is_false_wake("turn on the kitchen lights", _rec()) is False

    def test_two_sentences_under_word_threshold_passes(self):
        # 2 sentences but only 7 words = under the multi-sentence threshold.
        assert is_false_wake("ok. turn the lights on now.", _rec()) is False

    def test_segment_shape_single_long_run_on(self):
        # No terminal punctuation; signal 3 doesn't fire; signal 4 does.
        text = (
            "well I think we should go to the store and get some milk "
            "and then maybe stop by"
        )
        segs = [{"t0_ms": 0, "t1_ms": 4500}]
        assert is_false_wake(text, _rec(duration=4.5), segments=segs) is True


# ---------------------------------------------------------------------------
# Edge cases — empty / whitespace / unicode quirks that have shown up
# in prod logs at one time or another.
# ---------------------------------------------------------------------------


class TestIsFalseWakeEdgeCases:
    def test_empty_transcript(self):
        # Upstream filters should already block this, but defense in depth.
        assert is_false_wake("", _rec()) is False

    def test_whitespace_only_transcript(self):
        assert is_false_wake("   \n  ", _rec()) is False

    def test_no_recording_metadata_still_safe(self):
        # hit_max=False, duration arbitrary — short text is fine.
        assert is_false_wake("yes", _rec(duration=0.3)) is False
