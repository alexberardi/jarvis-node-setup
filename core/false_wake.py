"""False-wake detector — turns ambient room speech into a silent abort
instead of a CC round-trip.

A "false wake" is openWakeWord firing on something that isn't a command
addressed to Jarvis: TV audio, side conversation, the dog barking through
a phonetically-similar word.

**Scope, deliberately narrow (2026-07-20).** This layer only catches what
it can judge WITHOUT guessing at intent. The real "is this for me?" verdict
belongs in command-center, where the LLM sees the tool list and gets a
`[direction hint: ...]` derived from the node's pre-wake VAD — how much
speech-like audio preceded the wake. That is a far better signal than the
shape of a sentence, and a `<not_for_me/>` there fails silently and re-arms.

Two shape signals used to live here and were removed:

  - multi-sentence past ~8 words
  - whisper segment shape (one long run-on, or gap-less multi-segment)

Both encoded "real commands are short", which is simply false — a
phone-call request ("order a pepperoni pizza from J&G Pizza to be
delivered to my house") is inherently long and fluent, arrives as one
5-second segment, and was silently dropped. That failed CLOSED: dropping
at the node meant the better classifier downstream never saw the
utterance at all. Word count is not evidence of intent.

Two signals remain, both grounded in something observed rather than inferred:

  1. Abort phrases — the user heard the chime and is bailing ("cancel",
     "nevermind", "sorry jarvis", ...). Explicit intent. Wins outright.
  2. Recording hit max + ambient-shape transcript — the speaker never
     paused, so the listener cut at ``max_seconds``, AND the text looks
     overheard (>20 words OR starts mid-sentence in lowercase). Running
     into the cap is an acoustic fact, not a guess about phrasing.

Everything else goes to command-center. That costs a round trip on
genuinely ambient speech; the direction hint exists to make that call
correctly, and a mistaken drop here is worse than a mistaken round trip.

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


# NOTE: count_sentences and looks_like_narration_by_segments are no longer
# wired into is_false_wake — the signals that used them were removed (see the
# module docstring). They are kept deliberately: both are pure, tested, and
# carry thresholds tuned against real recordings, so they are the natural
# building blocks if a future classifier wants shape as ONE input among
# several rather than as a veto on its own.
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

    See the module docstring for the two remaining signals, and for why the
    shape-based ones were removed. ``segments`` is kept in the signature —
    callers still pass it and a future acoustic signal may want it — but is
    deliberately unused: whisper segment shape measured fluency, not intent.
    """
    raw = transcription.strip()
    text = raw.lower()

    # Signal 1: abort phrases — explicit user intent.
    for phrase in _ABORT_PHRASES:
        if text == phrase or text.startswith(phrase):
            return True

    # Signal 2: the recording ran into max_seconds, so the speaker never
    # paused, AND the text reads as overheard. Both halves required.
    if recording.hit_max_duration:
        words = text.split()
        # Long transcription — ambient conversation, not a command.
        if len(words) > 20:
            return True
        # Starts mid-sentence (lowercase in original, not "i" or "ok").
        if raw and raw[0].islower() and not text.startswith(("i ", "i'", "ok")):
            return True

    # Anything else is command-center's call: it has the tool list and the
    # pre-wake direction hint. Dropping here would deny it the chance.
    return False
