"""Tests for the conversation-filter helpers used by the follow-up loop.

Three independent decisions live here:

  * ``looks_like_self_echo`` — did the mic just hear our own TTS instead
    of the user? Canonical case is the "Here is sad music" beta blocker
    where Whisper transcribes the assistant's tail.
  * ``extract_assistant_text`` — pull the spoken text out of CC's result
    dict so the echo check has a reference signal.
  * ``is_follow_up_noise`` — drop transcripts that look like ambient
    noise (exact repeat across iterations or a lone non-command word).

Each is pure logic and is tested independently with a corpus that
includes the specific bugs they were added to fix.
"""

import pytest

from core.conversation_filters import (
    extract_assistant_text,
    is_follow_up_noise,
    looks_like_self_echo,
)


# ---------------------------------------------------------------------------
# looks_like_self_echo
# ---------------------------------------------------------------------------


class TestLooksLikeSelfEcho:
    """Self-echo guard — high-overlap stopword-stripped comparison."""

    def test_empty_text_returns_false(self):
        assert looks_like_self_echo("", "I'll set the timer.") is False

    def test_empty_assistant_returns_false(self):
        # No reference signal — echo check sits dormant rather than
        # producing false positives.
        assert looks_like_self_echo("turn on the lights", "") is False

    def test_both_empty_returns_false(self):
        assert looks_like_self_echo("", "") is False

    def test_under_two_significant_words_returns_false(self):
        # All-stopword content can't meet the >=2 threshold.
        assert looks_like_self_echo("the and you", "anything") is False

    def test_canonical_beta_blocker_here_is_sad_music(self):
        # The May 2026 beta blocker: assistant said "Here is some sad
        # music for you." TTS tail bled through, Whisper transcribed it,
        # follow-up loop treated it as the user requesting "sad music"
        # again, producing a phantom turn. Stopwords {here, is, some, for,
        # you} strip to {sad, music} on both sides → 100% overlap.
        user_text = "Here is sad music"
        assistant_text = "Here is some sad music for you."
        assert looks_like_self_echo(user_text, assistant_text) is True

    def test_two_word_partial_overlap_is_not_echo(self):
        # Two significant words but only one shared — the 100%-on-2 rule
        # protects against false positives where the user happens to
        # repeat one keyword.
        user_text = "sad story"
        assistant_text = "Here is some sad music for you."
        # significant user words: {sad, story}; assistant: {sad, music}
        # overlap = 1, len = 2 → not echo
        assert looks_like_self_echo(user_text, assistant_text) is False

    def test_three_word_full_overlap_is_echo(self):
        user_text = "set kitchen timer"
        assistant_text = "Okay, set the kitchen timer for ten minutes."
        # significant user words: {set, kitchen, timer}; all in assistant.
        # overlap = 3/3 = 100% >= 85% → echo
        assert looks_like_self_echo(user_text, assistant_text) is True

    def test_three_word_below_threshold_is_not_echo(self):
        # Real follow-up that quotes one word — must not suppress.
        user_text = "yes set for eight pm"
        assistant_text = "I'll set the alarm for eight PM."
        # user significant: {yes, set, eight, pm}; assistant: {set, alarm, eight, pm}
        # overlap = 3/4 = 75% < 85% → not echo
        assert looks_like_self_echo(user_text, assistant_text) is False

    def test_case_insensitive(self):
        assert looks_like_self_echo(
            "HERE IS SAD MUSIC",
            "here is some sad music for you.",
        ) is True

    def test_short_words_under_two_chars_dropped(self):
        # Single-letter tokens like "a" / "I" filtered by len >= 2 check.
        assert looks_like_self_echo("a b c d", "a b c d") is False


# ---------------------------------------------------------------------------
# extract_assistant_text
# ---------------------------------------------------------------------------


class TestExtractAssistantText:
    """Field-name probe across CC's slightly varying response shapes."""

    def test_none_returns_empty(self):
        assert extract_assistant_text(None) == ""

    def test_non_dict_returns_empty(self):
        # Defensive — the type hint says dict|None but the callsite passes
        # whatever the upstream gave it.
        assert extract_assistant_text("a string") == ""  # type: ignore[arg-type]
        assert extract_assistant_text(42) == ""           # type: ignore[arg-type]

    def test_empty_dict_returns_empty(self):
        assert extract_assistant_text({}) == ""

    def test_message_key_wins(self):
        # First key in the probe order — used by the unified-voice path.
        result = {"message": "the spoken bit", "text": "alt"}
        assert extract_assistant_text(result) == "the spoken bit"

    def test_falls_through_to_text(self):
        assert extract_assistant_text({"text": "alt"}) == "alt"

    def test_falls_through_to_response(self):
        assert extract_assistant_text({"response": "via response key"}) == "via response key"

    def test_falls_through_to_spoken_text(self):
        assert extract_assistant_text({"spoken_text": "via spoken_text"}) == "via spoken_text"

    def test_falls_through_to_assistant_text(self):
        assert extract_assistant_text({"assistant_text": "via assistant_text"}) == "via assistant_text"

    def test_empty_string_value_falls_through(self):
        # Empty-after-strip is treated as "key absent" so the probe
        # continues — protects the echo check from a false positive on
        # vacuous responses.
        result = {"message": "  ", "text": "real text"}
        assert extract_assistant_text(result) == "real text"

    def test_none_value_falls_through(self):
        result = {"message": None, "text": "real text"}
        assert extract_assistant_text(result) == "real text"

    def test_unknown_keys_return_empty(self):
        # Unknown shape → empty → echo check stays dormant. Graceful.
        assert extract_assistant_text({"random_other_key": "x"}) == ""


# ---------------------------------------------------------------------------
# is_follow_up_noise
# ---------------------------------------------------------------------------


class TestIsFollowUpNoise:
    """Narrower than is_non_speech — runs only in follow-up where we're
    skeptical about whether audio was directed at the device."""

    def test_empty_text_is_noise(self):
        assert is_follow_up_noise("", None) is True

    def test_whitespace_text_is_noise(self):
        # `not text` is the explicit empty/None check; whitespace passes
        # through, gets stripped to "" later, len(words)==0 (not == 1).
        # Production behavior: this returns False. Locking that in here.
        # (We're characterizing the existing behavior, not designing it.)
        assert is_follow_up_noise("   ", None) is False

    def test_exact_repeat_is_noise(self):
        assert is_follow_up_noise("turn on the lights", "turn on the lights") is True

    def test_exact_repeat_case_insensitive(self):
        assert is_follow_up_noise("Turn on the lights", "turn on the LIGHTS") is True

    def test_exact_repeat_with_trailing_punct(self):
        # Both sides strip ".!,?" from the end before comparing.
        assert is_follow_up_noise("yes.", "yes!") is True

    @pytest.mark.parametrize("word", [
        "kitchen", "Kitchen.", "okay", "hello", "uh",
        "stop", "yes", "no", "thanks",
    ])
    def test_single_words_are_never_noise(self, word):
        # One-word answers ("Kitchen." to "kitchen or oven?") are real.
        assert is_follow_up_noise(word, None) is False

    def test_multi_word_passes(self):
        assert is_follow_up_noise("turn off the lights", None) is False

    def test_different_text_with_prev_is_not_repeat_noise(self):
        # Real follow-ups distinct from the previous transcription pass
        # the repeat check, then pass the multi-word check.
        assert is_follow_up_noise(
            "and the dining room too",
            "turn on the kitchen lights",
        ) is False
