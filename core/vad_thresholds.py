"""VAD / barge-in / silence threshold helpers — Config wrappers + one
piece of real math.

Four read-only Config wrappers (``barge_in_enabled``,
``barge_in_threshold``, ``barge_in_energy_threshold``,
``pre_wake_vad_threshold``) carry the production defaults that a fresh
node has to fire correctly with before any per-room tuning. Each default
encodes a beta-incident lesson — see inline.

:func:`adaptive_silence_threshold` is the only real logic here: it
derives a per-cycle silence threshold from the pre-wake noise floor so
the recorder stops in the right place regardless of how loud the room
currently is. Bounded clamp protects against both runaway-low (recorder
never stops) and runaway-high (clips multi-syllable commands).
"""

from __future__ import annotations

from utils.config_service import Config


def barge_in_enabled() -> bool:
    """Opt-out: a fresh node ships with barge-in on."""
    raw = Config.get_str("barge_in_enabled", "true") or "true"
    return raw.lower() in ("true", "1", "yes")


def barge_in_threshold() -> float:
    """OpenWakeWord score threshold during TTS playback.

    Was 0.07, lowered to 0.04 after beta logs showed real
    "Hey Jarvis"-over-TTS scores peaking at 0.05-0.10 (right at the old
    floor). confirm_chunks=2 plus the recent_max_rms energy gate keep
    false positives bounded at this lower threshold.
    """
    return Config.get_float("barge_in_threshold", 0.04)


def barge_in_energy_threshold() -> float:
    """RMS gate (int16) that must be cleared before a barge-in score
    even counts. Prevents TTS bleed-through from triggering barge-in
    on its own residual energy."""
    return Config.get_float("barge_in_energy_threshold", 500.0)


def pre_wake_vad_threshold() -> float:
    """RMS (int16) above which a 80 ms frame counts as speech-like.

    The dev-Pi USB mic showed baseline-ambient RMS sustained in the
    high-hundreds-to-low-thousands range, so the original 500 default
    flagged ordinary "quiet room" frames as speech. 2500 separates that
    ambient floor from a person actually speaking. Per-room mic tuning
    lives in the ``pre_wake_vad_rms_threshold`` setting so we can adjust
    without a redeploy once we have observed values.
    """
    return Config.get_float("pre_wake_vad_rms_threshold", 2500.0)


def adaptive_silence_threshold(rms_stats: dict[str, float]) -> int | None:
    """Derive a per-cycle silence_threshold from the pre-wake noise floor.

    The 5 s pre-wake RMS window is overwhelmingly ambient (the wake word
    itself is only the last ~0.25 s), so its median is a clean read of
    room noise at the instant of wake. We lift the threshold to a
    multiple of that floor so normal speech (typically RMS 1000-3000+)
    easily clears it but breath/HVAC/fridge-hum bursts don't — without
    forcing the operator to hand-tune a static value that's right for
    one time of day and wrong for another (the kitchen-Pi failure mode
    that motivated this).

    Returns ``None`` to mean "fall back to the static config value":
    either auto-mode is disabled, the stats deque was empty, or the
    multiplier produced something obviously wrong.

    Bounds: ``[200, 1000]``. Below 200 the recorder treats sub-baseline
    HVAC ticks as silence-breaks and never stops; above 1000 it starts
    cutting into normal speech amplitude (kitchen with fan/TV produced
    a 470 median → 3.0 × → 1410 under the old ceiling, which clipped
    multi-syllable commands mid-sentence and Whisper hallucinated short
    words like "Bye." that hit the filter). Bias is toward NOT clipping
    the user: a too-low threshold lengthens the recording (recoverable),
    a too-high one drops the command (not recoverable).
    """
    if not Config.get_bool("silence_threshold_auto", True):
        return None
    if not isinstance(rms_stats, dict):
        return None
    median = rms_stats.get("median")
    if not isinstance(median, (int, float)) or median <= 0:
        return None
    multiplier = Config.get_float("silence_threshold_auto_multiplier", 2.0)
    floor = Config.get_int("silence_threshold_auto_floor", 200)
    ceiling = Config.get_int("silence_threshold_auto_ceiling", 1000)
    return max(floor, min(ceiling, int(median * multiplier)))
