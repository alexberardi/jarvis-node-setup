"""Transcript + wake-gate helpers shared by voice_listener.

Lives outside ``scripts/voice_listener.py`` so it can be imported without
pulling in pyaudio, openwakeword, sqlcipher3, the AEC C bindings, etc. —
all of which the wake loop needs at runtime but tests do not. Module-level
state (``_wake_min_next_ts``) is intentionally shared with voice_listener
via this single import — Python module singletons give us the cross-module
shared state for free.

Why the helpers are HERE and not in ``utils/``: ``utils/`` is reserved
for node-framework code; ``core/`` is where audio / wake-cycle primitives
live (``core/barge_in.py``, ``core/audio_bus.py``, ``core/platform_audio.py``).
This module is the wake-cycle's transcript + suppression policy.
"""

from __future__ import annotations

import re
import threading

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


# ---------------------------------------------------------------------------
# Transcript filter
# ---------------------------------------------------------------------------

# Entire transcript is one or more bracketed annotations separated by
# whitespace / simple punctuation. Catches ``*sniff*``, ``[BLANK_AUDIO]``,
# ``(wind blowing)``, ``<inaudible>``, ``*sad noises* *cough*``, and any
# mix. Asterisk-bracketed forms were missed by the original ``startswith
# ('[' or '(')`` check — the 2026-06-02 prod incident saw ``*sniff*`` reach
# CC, where the LLM hallucinated "I smell something burning."
_BRACKETED_ANNOTATION_RE = re.compile(
    r"^\s*(?:[\*\[\(\<][^\*\]\)\>]+[\*\]\)\>]\s*[\.,!?\-]?\s*)+\.?\s*$",
)


def is_non_speech(text: str | None) -> bool:
    """True if Whisper output is a non-transcript — empty, whitespace, or
    a bracketed annotation like ``[BLANK_AUDIO]`` / ``(wind blowing)`` /
    ``*sniff*`` / ``<inaudible>`` that Whisper emits for silence and
    noise rather than user speech.

    This NO LONGER filters "hallucination phrases" (single-word fillers,
    YouTube artifacts, etc.). That blocklist was eating real commands
    like "okay" / "bye" / "yes". The right place to decide "is this a
    real command" is CC's LLM ``<not_for_me/>`` classifier, which has
    full context — speaker, prior turns, available tools — that a static
    word list never will. We only drop things that aren't transcripts at
    all here; anything that looks like words goes to CC.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    # Trim trailing sentence punctuation Whisper sometimes appends so
    # ``*sniff*.`` is treated the same as ``*sniff*``.
    body = stripped.rstrip(".,!?")
    if body and _BRACKETED_ANNOTATION_RE.match(body):
        return True
    # "..." is a Whisper non-speech marker, not an utterance.
    if stripped.strip(".") == "":
        return True
    return False


# ---------------------------------------------------------------------------
# Wake-acceptance gate — same-utterance debounce only
# ---------------------------------------------------------------------------

# Single monotonic timestamp marking the earliest moment a wake fire is
# allowed to be accepted. The check happens inline at the wake-fire site
# in voice_listener; on every accepted wake the site advances the gate by
# ``_WAKE_DEBOUNCE_SEC``. This exists only to prevent openWakeWord's same
# "Hey Jarvis" from firing twice on consecutive 80 ms chunks — a hardware
# property of the wake-word model, not a policy decision.
#
# There USED to be a second, much longer cool-down armed after a CC
# "not_for_me" verdict (20 s, escalating to 60 s on clusters). It was
# removed: locking the room out for tens of seconds because a probabilistic
# classifier might have been wrong is the wrong abstraction. Misclassifies
# now silently skip TTS and the user can re-wake immediately. See
# voice_listener for the verdict handler.
_WAKE_DEBOUNCE_SEC = 8.0
_wake_min_next_ts: float = 0.0
_wake_gate_lock = threading.Lock()


def get_wake_min_next_ts() -> float:
    """Read the current gate cutoff. Callers compare against
    ``time.monotonic()`` to decide whether a wake fire is accepted."""
    return _wake_min_next_ts


def reset_wake_gate() -> None:
    """Drop the gate cutoff. Test-only."""
    global _wake_min_next_ts
    _wake_min_next_ts = 0.0
