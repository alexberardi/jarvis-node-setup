"""False-wake detector — turns ambient room speech into a silent abort
instead of a CC round-trip.

A "false wake" is openWakeWord firing on something that isn't a command
addressed to Jarvis: TV audio, side conversation, the dog barking through
a phonetically-similar word. We catch these AFTER the listen window so
we can use both the transcript and the whisper segment timing.

Four signals, evaluated in order:

  1. Abort phrases — user heard the chime and is bailing ("cancel",
     "nevermind", "sorry jarvis", ...). Wins outright.
  2. Recording hit max + ambient-shape transcript — speaker never paused
     (so the listener cut at ``max_seconds``) AND the text looks like
     overheard speech (>20 words OR starts mid-sentence in lowercase).
  3. Multi-sentence + word count past threshold — real commands are
     overwhelmingly single-sentence; ≥2 sentences past ~8 words is almost
     always overheard narration.
  4. Whisper segment shape — one long run-on (≥4 s single segment) OR
     gap-less multi-segment (all gaps <300 ms) past the word threshold.

Signals 3 + 4 are overridden by direct addressing ("jarvis" anywhere
in the text) — a real multi-sentence request that names the assistant
counts as a command even when it looks narrative.

The detector is intentionally pure — RecordingResult + transcript +
optional segments in, bool out. All thresholds live in this module as
module-level constants so they're visible without diving through Config.
"""

from __future__ import annotations

import re

from scripts.speech_to_text import RecordingResult


_ABORT_PHRASES: set[str] = {
    "never mind", "nevermind", "cancel", "forget it",
    "that wasn't for you", "not you", "sorry jarvis",
    "ignore that", "ignore me",
}

_FALSE_WAKE_MULTI_SENTENCE_WORD_THRESHOLD = 8
_FALSE_WAKE_NARRATION_SINGLE_SEGMENT_MIN_MS = 4000
_FALSE_WAKE_NARRATION_MAX_GAP_MS = 300
_SENTENCE_END_RE = re.compile(r"[.!?]+(?=\s|$)")


def count_sentences(raw: str) -> int:
    """Count sentence-ending boundaries in a transcript.

    Whisper punctuates fairly reliably in English mode. We count terminal
    punctuation runs (``.``, ``!``, ``?``) anchored before whitespace or
    end-of-string — ``...`` mid-string counts as one boundary, not three.
    """
    return len(_SENTENCE_END_RE.findall(raw))


def looks_like_narration_by_segments(
    word_count: int, segments: list[dict] | None
) -> bool:
    """Whisper segment-timing check for run-on ambient speech.

    Real commands tend to be either (a) a single short segment or
    (b) multiple segments with real pauses between thoughts that
    whisper.cpp picks up as boundaries. Continuous ambient narration
    comes back as either one long segment or several segments stitched
    with sub-300ms transitions. Returns True for either shape past the
    word threshold.

    Empty / missing segments → False (older whisper-api builds don't
    expose timing; we don't penalize them).
    """
    if word_count <= _FALSE_WAKE_MULTI_SENTENCE_WORD_THRESHOLD:
        return False
    if not segments:
        return False

    if len(segments) == 1:
        seg = segments[0]
        duration_ms = int(seg.get("t1_ms", 0)) - int(seg.get("t0_ms", 0))
        return duration_ms >= _FALSE_WAKE_NARRATION_SINGLE_SEGMENT_MIN_MS

    max_gap = 0
    for prev, nxt in zip(segments, segments[1:]):
        gap = int(nxt.get("t0_ms", 0)) - int(prev.get("t1_ms", 0))
        if gap > max_gap:
            max_gap = gap
    return max_gap < _FALSE_WAKE_NARRATION_MAX_GAP_MS


def is_false_wake(
    transcription: str,
    recording: RecordingResult,
    segments: list[dict] | None = None,
) -> bool:
    """Decide if a wake-cycle should silently abort.

    See the module docstring for the four signals and their ordering.
    """
    raw = transcription.strip()
    text = raw.lower()

    # Signal 1: abort phrases
    for phrase in _ABORT_PHRASES:
        if text == phrase or text.startswith(phrase):
            return True

    # Signal 2: recording hit max duration (speaker never paused)
    if recording.hit_max_duration:
        words = text.split()
        # Long transcription — ambient conversation, not a command
        if len(words) > 20:
            return True
        # Starts mid-sentence (lowercase in original, not "i" or "ok")
        if raw and raw[0].islower() and not text.startswith(("i ", "i'", "ok")):
            return True

    # Direct addressing wins for both shape signals: a multi-sentence
    # request that names Jarvis ("jarvis, set a timer. ten minutes.")
    # is a real command even when it looks narrative.
    if "jarvis" in text:
        return False

    word_count = len(text.split())

    # Signal 3: narration shape — multiple sentences past the word threshold.
    # Real commands are overwhelmingly single-sentence; multi-sentence past
    # ~8 words almost always means we overheard someone talking nearby.
    if (
        count_sentences(raw) >= 2
        and word_count > _FALSE_WAKE_MULTI_SENTENCE_WORD_THRESHOLD
    ):
        return True

    # Signal 4: whisper segment shape (PRD #3, segment-level variant).
    if looks_like_narration_by_segments(word_count, segments):
        return True

    return False
