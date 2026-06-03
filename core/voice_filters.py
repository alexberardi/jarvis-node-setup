"""Transcript + wake-gate helpers shared by voice_listener.

Lives outside ``scripts/voice_listener.py`` so it can be imported without
pulling in pyaudio, openwakeword, sqlcipher3, the AEC C bindings, etc. —
all of which the wake loop needs at runtime but tests do not. Module-level
state (``_wake_min_next_ts``, ``_not_for_me_history``) is intentionally
shared with voice_listener via this single import — Python module
singletons give us the cross-module shared state for free.

Why the helpers are HERE and not in ``utils/``: ``utils/`` is reserved
for node-framework code; ``core/`` is where audio / wake-cycle primitives
live (``core/barge_in.py``, ``core/audio_bus.py``, ``core/platform_audio.py``).
This module is the wake-cycle's transcript + suppression policy.
"""

from __future__ import annotations

import re
import threading
import time

from jarvis_log_client import JarvisLogger

from utils.config_service import Config

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
# Wake-acceptance gate
# ---------------------------------------------------------------------------

# Single monotonic timestamp marking the earliest moment a wake fire is
# allowed to be accepted; checks happen at the wake-fire site in
# voice_listener and any caller can push the gate forward via
# ``suppress_wake_for``. Two suppression sources today:
#
# 1. Same-utterance double-fire: openWakeWord can score >threshold on
#    consecutive 80ms chunks for one "Hey Jarvis". On every accepted
#    wake we push the gate to ``now + _WAKE_DEBOUNCE_SEC``.
# 2. Post-not_for_me quiet period: when the server signals an ambient
#    false-wake, side conversations cluster — the next sentence of the
#    same conversation will likely re-trigger wake within seconds.
#    The not_for_me handler calls ``suppress_wake_for`` with a longer
#    cool-down (configurable via ``not_for_me_quiet_seconds``).
#
# Multiple suppressions don't stack — the later/longer one wins.
_WAKE_DEBOUNCE_SEC = 8.0
_NOT_FOR_ME_QUIET_SEC_DEFAULT = 20.0
# Multi-fire escalation: side conversations cluster — if we see N
# not_for_me events within a rolling window we lengthen the suppression
# so the same conversation doesn't keep re-firing wake every few seconds.
# A single misfire is a one-off; two in a row says the room is loud.
_NOT_FOR_ME_HISTORY_WINDOW_SEC = 30.0
_NOT_FOR_ME_ESCALATION_COUNT = 2
_NOT_FOR_ME_ESCALATED_SEC_DEFAULT = 60.0
_wake_min_next_ts: float = 0.0
_wake_gate_lock = threading.Lock()
_not_for_me_history: list[float] = []


def get_wake_min_next_ts() -> float:
    """Read the current gate cutoff. Callers compare against
    ``time.monotonic()`` to decide whether a wake fire is accepted."""
    return _wake_min_next_ts


def reset_wake_gate() -> None:
    """Drop the gate cutoff and clear the not_for_me history. Test-only."""
    global _wake_min_next_ts
    _wake_min_next_ts = 0.0
    _not_for_me_history.clear()


def suppress_wake_for(seconds: float, reason: str = "") -> None:
    """Push the wake-acceptance gate forward by ``seconds`` from now.

    No-op if the gate is already further out. Logs the suppression so
    the source is debuggable from journalctl.
    """
    global _wake_min_next_ts
    if seconds <= 0:
        return
    target = time.monotonic() + seconds
    with _wake_gate_lock:
        if target > _wake_min_next_ts:
            _wake_min_next_ts = target
            logger.info(
                "wake-gate-extended",
                seconds=round(seconds, 1),
                reason=reason or "unspecified",
            )


def record_not_for_me_event() -> float:
    """Record a not_for_me fire and return the cool-down to apply.

    Returns the standard ``not_for_me_quiet_seconds`` for an isolated
    event, or the escalated ``not_for_me_escalated_quiet_seconds`` when
    ``_NOT_FOR_ME_ESCALATION_COUNT`` events have fired within the rolling
    ``_NOT_FOR_ME_HISTORY_WINDOW_SEC`` window. Side conversations cluster
    — a second hit inside the window says the next wake is also likely
    overheard speech, so we widen the gate instead of letting the same
    cluster keep re-firing.
    """
    now = time.monotonic()
    cutoff = now - _NOT_FOR_ME_HISTORY_WINDOW_SEC
    with _wake_gate_lock:
        _not_for_me_history[:] = [t for t in _not_for_me_history if t >= cutoff]
        _not_for_me_history.append(now)
        count = len(_not_for_me_history)
    if count >= _NOT_FOR_ME_ESCALATION_COUNT:
        cooldown = Config.get_float(
            "not_for_me_escalated_quiet_seconds",
            _NOT_FOR_ME_ESCALATED_SEC_DEFAULT,
        )
        logger.info(
            "not-for-me cluster detected — escalating cool-down",
            count=count,
            window_seconds=_NOT_FOR_ME_HISTORY_WINDOW_SEC,
            cooldown_seconds=round(cooldown, 1),
        )
        return cooldown
    return Config.get_float(
        "not_for_me_quiet_seconds",
        _NOT_FOR_ME_QUIET_SEC_DEFAULT,
    )
