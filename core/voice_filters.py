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
# Wake-acceptance gate — same-utterance debounce + not_for_me SOFT cool-down
# ---------------------------------------------------------------------------

# Single monotonic timestamp marking the earliest moment a wake fire is
# allowed to be accepted. The check happens inline at the wake-fire site
# in voice_listener; on every accepted wake the site advances the gate by
# ``_WAKE_DEBOUNCE_SEC``. The debounce exists to prevent openWakeWord's
# same "Hey Jarvis" from firing twice on consecutive 80 ms chunks — a
# hardware property of the wake-word model, not a policy decision.
#
# HISTORY, because this gate has flip-flopped and the next reader needs
# the whole arc: a 20s→60s HARD cool-down after a CC "not_for_me" verdict
# shipped 2026-06-02, then was removed 2026-06-04 ("locking the room out
# because a probabilistic classifier might have been wrong is the wrong
# abstraction"). Both positions were half right. Without any cool-down,
# a kitchen full of conversation re-fires wake every few sentences, each
# one a full STT+CC round-trip that CC may answer (prod kitchen node,
# 2026-08-15). With a hard cool-down, one misclassified real question
# locks the user out for 20s.
#
# The current design is the synthesis — a SOFT cool-down: after a
# not_for_me verdict the gate arms for ``not_for_me_quiet_seconds``
# (escalating on clusters), but a wake whose score clears
# ``not_for_me_override_threshold`` (default 0.6, vs the ~0.4 base
# threshold) punches through immediately. Ambient speech that barely
# grazes the base threshold stays suppressed; a deliberate, clearly
# spoken "Hey Jarvis" — which scores 0.66–0.99 in practice (see
# wake_detector docstring) — is never locked out.
_WAKE_DEBOUNCE_SEC = 8.0
_wake_min_next_ts: float = 0.0
# Score that bypasses the gate while it's armed. None = hard gate (the
# same-utterance debounce is always hard; only the not_for_me cool-down
# arms a soft override).
_wake_gate_override_threshold: float | None = None
_wake_gate_lock = threading.Lock()

# Rolling history of recent not_for_me verdicts (monotonic timestamps),
# used to escalate the cool-down when a side conversation keeps firing
# wake: one stray hit costs ``not_for_me_quiet_seconds``; a cluster
# (>=2 within the escalation window) costs the escalated duration.
_not_for_me_history: list[float] = []


def get_wake_min_next_ts() -> float:
    """Read the current gate cutoff. Callers compare against
    ``time.monotonic()`` to decide whether a wake fire is accepted."""
    return _wake_min_next_ts


def get_wake_gate_override_threshold() -> float | None:
    """Score that bypasses an armed gate, or None when the gate is hard."""
    return _wake_gate_override_threshold


def reset_wake_gate() -> None:
    """Drop the gate cutoff + history. Test-only."""
    global _wake_min_next_ts, _wake_gate_override_threshold
    _wake_min_next_ts = 0.0
    _wake_gate_override_threshold = None
    _not_for_me_history.clear()


def arm_not_for_me_cooldown(*, now_mono: float, config_get_float) -> float:
    """Arm the soft cool-down after a CC ``not_for_me`` verdict.

    Records the event in the rolling history, picks the base or escalated
    duration, and pushes the gate forward (never backward — an armed
    longer gate is not shortened by a later shorter one). Returns the
    applied cool-down seconds for logging.

    ``config_get_float`` is injected (``Config.get_float``) so this
    module keeps its no-heavy-imports property for tests.
    """
    global _wake_min_next_ts, _wake_gate_override_threshold

    quiet = config_get_float("not_for_me_quiet_seconds", 20.0)
    if quiet <= 0:
        return 0.0  # operator opt-out — behave like the gate never armed
    escalated = config_get_float("not_for_me_escalated_quiet_seconds", 60.0)
    window = config_get_float("not_for_me_escalation_window_seconds", 120.0)
    override = config_get_float("not_for_me_override_threshold", 0.6)

    with _wake_gate_lock:
        _not_for_me_history.append(now_mono)
        # Prune events older than the escalation window.
        cutoff = now_mono - window
        _not_for_me_history[:] = [t for t in _not_for_me_history if t >= cutoff]
        cooldown = escalated if len(_not_for_me_history) >= 2 else quiet

        armed_until = now_mono + cooldown
        if armed_until > _wake_min_next_ts:
            _wake_min_next_ts = armed_until
        _wake_gate_override_threshold = override

    logger.info(
        "not-for-me-cooldown-armed",
        cooldown_sec=cooldown,
        recent_events=len(_not_for_me_history),
        override_threshold=override,
    )
    return cooldown
