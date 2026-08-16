"""Conversation-filter helpers used by the follow-up loop.

Three independent decisions live here, each pure logic over text:

  * :func:`looks_like_self_echo` — did the mic just hear our own TTS
    instead of the user? Compares stopword-stripped word sets against
    the assistant's last spoken text; high overlap = echo. Secondary
    defense behind the post-TTS settle delay; catches the May 2026
    "Here is sad music" beta blocker where PA buffer drain ran long.

  * :func:`extract_assistant_text` — best-effort pull of the spoken
    text out of CC's result dict. CC's voice/command response shapes
    vary slightly between code paths; we probe likely keys and fall
    back to empty so the echo check sits dormant on unknown shapes.

  * :func:`is_follow_up_noise` — narrower than
    ``core.voice_filters.is_non_speech``. Runs only in follow-up where
    we're skeptical about whether audio was directed at the device.
    Catches exact repeats across iterations (Whisper hallucinating the
    same phrase from similar ambient noise) and lone non-command words.
"""

from __future__ import annotations

import re


# Words that are valid as standalone single-word follow-ups (control
# verbs the LLM doesn't need to disambiguate, plus bare confirmations/
# denials — CC asks yes/no questions in follow-up and suppressing the
# one-word answer strands the exchange). Used by the follow-up loop's
# local noise gate, not by is_non_speech.
_VALID_FOLLOW_UP_WORDS: set[str] = {
    "stop", "pause", "resume", "help", "repeat",
    "louder", "quieter", "cancel", "continue",
    "yes", "no", "yeah", "sure",
}


_ECHO_STOPWORDS: set[str] = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "and", "or", "for", "in", "on", "at", "by", "with",
    "i", "you", "it", "we", "they", "he", "she", "me", "us", "them",
    "this", "that", "these", "those", "here", "there",
    "do", "does", "did", "have", "has", "had", "will", "would", "can",
    "could", "should", "may", "might", "must",
    "what", "when", "where", "why", "how", "who",
}


_WORD_RE = re.compile(r"[a-z']+")


def looks_like_self_echo(text: str, last_assistant_text: str) -> bool:
    """True if the follow-up transcript looks like the node hearing its
    own TTS response instead of the user.

    Heuristic: compare significant (non-stopword) word sets.

    * ≥3 significant user-side words and ≥85% overlap → echo.
    * 2 significant user-side words and 100% overlap → echo (this is
      what catches the "Here is sad music" canonical case: stopwords
      strip to {sad, music}, both already in the assistant's reply).

    The threshold is intentionally high so a legitimate user reply that
    quotes part of the assistant ("yes, set it for 8 PM" against
    "I'll set the alarm for 8 PM") doesn't get suppressed — that case
    overlaps on {set, eight, pm} which is 3/4 = 75%, below 85%.
    """
    if not text or not last_assistant_text:
        return False
    user_words = {w for w in _WORD_RE.findall(text.lower()) if len(w) >= 2}
    user_words -= _ECHO_STOPWORDS
    if len(user_words) < 2:
        return False
    assistant_words = {
        w for w in _WORD_RE.findall(last_assistant_text.lower()) if len(w) >= 2
    }
    overlap = len(user_words & assistant_words)
    if len(user_words) == 2:
        return overlap == 2
    return overlap / len(user_words) >= 0.85


def extract_assistant_text(result: dict | None) -> str:
    """Best-effort extraction of the spoken text from a CC result dict.

    CC's voice/command response shapes vary slightly between code paths;
    we look at the most likely keys and fall back to empty. Empty means
    the echo check sits dormant (no false positives) — graceful.
    """
    if not isinstance(result, dict):
        return ""
    for key in ("message", "text", "response", "spoken_text", "assistant_text"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def is_follow_up_noise(text: str, prev_text: str | None) -> bool:
    """Detect ambient noise Whisper transcribed as speech during follow-up.

    More conservative than ``is_non_speech`` — this only runs in the
    follow-up loop where we're skeptical about whether audio was directed
    at the device. Catches two patterns:

    1. **Exact repeat** — Whisper hallucinating the same phrase from
       similar ambient noise on consecutive iterations.
    2. **Lone word** — Single generic words that aren't valid commands.
       Real follow-ups are almost never one isolated word.
    """
    if not text:
        return True

    stripped = text.strip()
    lowered = stripped.lower().rstrip(".!,?")
    words = lowered.split()

    # Exact repeat of previous transcription
    if prev_text:
        prev_lowered = prev_text.strip().lower().rstrip(".!,?")
        if lowered == prev_lowered:
            return True

    # Single word not in the valid-command set
    if len(words) == 1 and lowered not in _VALID_FOLLOW_UP_WORDS:
        return True

    return False
