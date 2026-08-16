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
        assert is_false_wake("nevermind that was nothing", _rec()) is True
        assert is_false_wake("forget it, sorry", _rec()) is True

    def test_abort_phrase_is_case_insensitive(self):
        assert is_false_wake("Cancel", _rec()) is True
        assert is_false_wake("NEVERMIND", _rec()) is True

    def test_cancel_only_matches_as_bare_utterance(self):
        # "cancel" is also an ordinary command verb. Prefix-matching it
        # killed real commands node-side — "cancel my 3pm meeting" died
        # here and CC never saw it. Only the bare word aborts; anything
        # longer is CC's call (fail-open: a wrong suppression is worse
        # than a wasted round trip).
        assert is_false_wake("cancel", _rec()) is True
        assert is_false_wake("Cancel.", _rec()) is True
        assert is_false_wake("cancel my 3pm meeting", _rec()) is False
        assert is_false_wake("cancel that please", _rec()) is False
        assert is_false_wake("cancel the timer", _rec()) is False


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
    """The shape signals are GONE (2026-07-20).

    Multi-sentence-past-8-words and whisper segment shape both encoded
    "real commands are short". They dropped long fluent commands at the
    node, where nothing downstream could recover them. These cases are now
    command-center's call: it sees the tool list and a pre-wake direction
    hint, and a <not_for_me/> there fails silently and re-arms.

    The corpus is KEPT rather than deleted — these are real recordings, and
    they are the regression set for whatever classifies them next.
    """

    def test_real_prod_false_wake_2026_06_03(self):
        # The actual transcript captured by prod kitchen at 18:41:07 —
        # ambient TV / family conversation that triggered a false wake.
        #
        # Capitalised, punctuated, 10 words: no acoustic tell at all, only
        # sentence shape. It now reaches CC, which should answer with
        # <not_for_me/> — this text is not a command in any tool's terms.
        # Worth watching in the kitchen: this is the case the node used to
        # absorb for free.
        text = "I'm playing the outside of it. I know a con."
        assert is_false_wake(text, _rec(duration=2.4)) is False

    def test_single_sentence_under_threshold_passes(self):
        # Short single-sentence command — the happy path.
        assert is_false_wake("turn on the kitchen lights", _rec()) is False

    def test_two_sentences_under_word_threshold_passes(self):
        # 2 sentences but only 7 words = under the multi-sentence threshold.
        assert is_false_wake("ok. turn the lights on now.", _rec()) is False

    def test_segment_shape_no_longer_drops_a_long_run_on(self):
        # Trails off mid-thought, so it really is overheard — but the only
        # evidence was one long segment, which is also what a fluent command
        # looks like. Deferred to CC rather than guessed at here.
        text = (
            "well I think we should go to the store and get some milk "
            "and then maybe stop by"
        )
        segs = [{"t0_ms": 0, "t1_ms": 4500}]
        assert is_false_wake(text, _rec(duration=4.5), segments=segs) is False


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


# ---------------------------------------------------------------------------
# Regression: the narration-shape signal must not punish fluent speech.
#
# Live 2026-07-19 — four consecutive phone-call requests were silently
# dropped. Each was a complete, correctly-transcribed sentence spoken
# without pausing, so whisper returned ONE segment over 4 s and the
# segment-shape signal called it ambient narration. Phone-call commands are
# inherently this long ("make an appointment at X for me this week"), so the
# feature was unusable by voice.
#
# The distinguishing evidence: hit_max_duration was False on every one — the
# listener stopped because the SPEAKER stopped. Narration runs into the cap.
# ---------------------------------------------------------------------------


class TestFluentCommandsSurvive:
    REAL_UTTERANCES = [
        "Can you make an appointment at Total Patient Care for me one day this week?",
        "Make an appointment at Total Patient Care one day this week.",
        "Can you call and make an appointment at Total Patient Care for this week?",
        "Call Tony's Pizzeria and order a large pepperoni for pickup.",
    ]

    @pytest.mark.parametrize("text", REAL_UTTERANCES)
    def test_long_single_segment_command_is_not_a_false_wake(self, text):
        """One 5 s segment, speaker stopped on their own → a command."""
        segments = [{"t0_ms": 0, "t1_ms": 5440}]
        assert is_false_wake(text, _rec(duration=5.44, hit_max=False), segments=segments) is False

    def test_narration_still_caught_when_recording_hit_the_cap(self):
        """Same shape, but the speaker never stopped → still ambient."""
        text = "so then he told me the whole story about the thing at work yesterday"
        segments = [{"t0_ms": 0, "t1_ms": 9000}]
        assert is_false_wake(text, _rec(duration=9.0, hit_max=True), segments=segments) is True

    def test_gapless_multi_segment_still_caught_at_the_cap(self):
        text = "and then she said we should go and I said maybe later this week sometime"
        segments = [
            {"t0_ms": 0, "t1_ms": 3000},
            {"t0_ms": 3100, "t1_ms": 6000},
            {"t0_ms": 6150, "t1_ms": 9000},
        ]
        assert is_false_wake(text, _rec(duration=9.0, hit_max=True), segments=segments) is True

    def test_gapless_multi_segment_survives_when_speaker_stopped(self):
        """A fluent multi-clause command with no long pauses is still a command."""
        # As whisper actually returns it: capitalised, terminally punctuated.
        text = "Call the pharmacy and ask them to refill my prescription for this month."
        segments = [
            {"t0_ms": 0, "t1_ms": 2500},
            {"t0_ms": 2600, "t1_ms": 5000},
        ]
        assert is_false_wake(text, _rec(duration=5.0, hit_max=False), segments=segments) is False

    def test_fragment_shape_is_no_longer_caught_without_hit_max(self):
        """Shape alone no longer decides.

        A lowercase, trailing-off fragment IS overheard speech, but the node
        cannot separate that from a real command without also punishing
        length — and this corpus writes real commands in lowercase too
        ("turn on the kitchen lights"), so a lowercase-start rule would drop
        them. Left to CC, which has better evidence.
        """
        text = (
            "well I think we should go to the store and get some milk "
            "and then maybe stop by"
        )
        assert is_false_wake(
            text, _rec(duration=4.5, hit_max=False),
            segments=[{"t0_ms": 0, "t1_ms": 4500}],
        ) is False

    def test_abort_phrases_still_fire_without_hit_max(self):
        """Explicit intent survives; inferred narration does not."""
        # Abort phrase — unambiguous, stays at the node.
        assert is_false_wake("never mind", _rec(), segments=None) is True
        # Multi-sentence narration: shape only, so CC decides now.
        assert is_false_wake(
            "he went to the store. then he came back and made dinner for us.",
            _rec(duration=6.0, hit_max=False),
            segments=[{"t0_ms": 0, "t1_ms": 6000}],
        ) is False


class TestLongCommandsSurvive:
    """Length is not evidence of intent.

    Live 2026-07-20: 'Order a pepperoni pizza from J&G Pizza to be delivered
    to my house.' transcribed perfectly, then died at the node — 13 words,
    one 5.8s whisper segment. Phone-call requests are inherently this long,
    which made the whole feature unusable by voice.
    """

    def test_the_dropped_pizza_order(self):
        text = (
            "Order a pepperoni pizza from J&G Pizza to be delivered "
            "to my house."
        )
        assert is_false_wake(
            text, _rec(duration=5.84, hit_max=False),
            segments=[{"t0_ms": 0, "t1_ms": 5840}],
        ) is False

    def test_the_dropped_appointment_request(self):
        # The 2026-07-19 variant, same shape, same silent drop.
        text = (
            "Can you make an appointment at Total Patient Care for me "
            "one day this week?"
        )
        assert is_false_wake(
            text, _rec(duration=5.0, hit_max=False),
            segments=[{"t0_ms": 0, "t1_ms": 5000}],
        ) is False

    def test_a_long_multi_sentence_command_survives(self):
        # Two sentences, 19 words, one segment: every old shape signal at
        # once. A person really does talk like this.
        text = (
            "Order a large pepperoni for pickup. Tell them it's under "
            "my name and I'll be there at six."
        )
        assert is_false_wake(
            text, _rec(duration=7.0, hit_max=False),
            segments=[{"t0_ms": 0, "t1_ms": 7000}],
        ) is False

    def test_a_recording_that_ran_into_the_cap_is_still_caught(self):
        # The one acoustic signal we kept: the speaker never stopped.
        text = (
            "so anyway i told him that the whole thing was going to fall "
            "apart and he just kept talking about the game and then she "
            "came in and said something about dinner"
        )
        assert is_false_wake(text, _rec(duration=7.0, hit_max=True)) is True
