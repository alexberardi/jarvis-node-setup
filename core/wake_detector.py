"""Wake-fire decision pipeline — threshold and debounce.

The wake-fire pipeline is the spine of every voice command. Two gates
in strict order:

  1. **Score gate** — ``oww.predict()`` must clear the configured static
     / auto-calibrated threshold. ONE threshold, whatever the household
     set; nothing lowers it behind their back. The music-profile swap
     (``wake_word_threshold_music``, 0.30 during self-playback) was
     removed 2026-08-26 — see the note below. It joins the retired
     June-2026 energy gate (lowered threshold + RMS-spike check +
     ``wake_music_trust_score`` bypass), which was dropped for actively
     discarding real wakes (PRD ``prds/wake-during-music.md``).

  2. **Debounce/cool-down gate** — atomic under
     ``voice_filters._wake_gate_lock``. If ``_wake_min_next_ts`` is in
     the future, suppress — UNLESS the gate is soft (armed by a CC
     ``not_for_me`` verdict) and the score clears the override
     threshold, in which case a deliberate, clearly spoken wake punches
     through. On any fire-through, advance the gate by
     ``_WAKE_DEBOUNCE_SEC`` (hard) and let the fire through.

The ``not_for_me`` cool-down was removed 2026-06-04 and restored as a
SOFT gate 2026-08-15 — see the voice_filters module docstring for the
full arc and rationale.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from jarvis_log_client import JarvisLogger

from core import voice_filters
from core.barge_in import oww_lock as _oww_lock
from core.voice_filters import _WAKE_DEBOUNCE_SEC
from core.wake_calibration import auto_calibrated_wake_threshold
from utils.config_service import Config


logger = JarvisLogger(service="jarvis-node")


# ---------------------------------------------------------------------------
# Threshold + oww-reset helpers
# ---------------------------------------------------------------------------


def current_wake_threshold() -> float:
    """THE single choke point for the wake-word detection threshold.

    Returns the static ``wake_word_threshold``, or the auto-calibrated
    value when ``wake_word_threshold_auto`` is on. Nothing else adjusts
    it — not playback, not energy, not a trust score.

    A ``"music"`` profile used to drop this to
    ``wake_word_threshold_music`` (0.30) during self-playback, to recover
    real wakes that loud music caps below the normal bar. It was removed
    on 2026-08-26 because it did not work.

    The design traded false fires for recall on an explicit premise —
    "extra false fires this admits are absorbed by CC verification (clip
    phrase-match), which is the fail-open backstop". That backstop never
    ran: ``voice.wake_verification_mode`` ships as ``bias`` (advisory)
    and the ``clip_unreliable`` verdict fails open, so the node lowered
    its bar by two thirds to pay a filter that was switched off.

    Measured on the prod kitchen node, 2026-08-16 → 08-26, joining node
    per-fire telemetry to CC wake-verify verdicts (fire counts and
    verdict counts reconciled exactly per day):

      - 47 music-profile fires → approximately ZERO verified wakes
      - days with the profile active: 12% of fires verified (8/68)
      - days without it:             69% of fires verified (40/58)
      - expected yield from the normal fires ALONE on those two days
        (21 × 0.69 = 14.5) already exceeded the 8 actually observed
      - 08-24 spent 38 extra fires to return 4 verified wakes all day

    It cost a flood and bought nothing. Waking over loud music is a job
    for a music-robust wake MODEL — see ``tools/wake_model_training/`` —
    not for a lower bar.
    """
    static_default = Config.get_float("wake_word_threshold", 0.4)
    if Config.get_bool("wake_word_threshold_auto", False):
        return auto_calibrated_wake_threshold(static_default)
    return static_default


def locked_oww_reset(oww_model) -> None:
    """Reset oww under the shared lock.

    Submitted to a background executor to keep the wake-hot-path
    unblocked while still serializing with any in-flight
    ``oww.predict()`` (the barge-in monitor also runs predict against
    the same model).
    """
    _t = time.monotonic()
    with _oww_lock:
        oww_model.reset()
    logger.info(
        f"⏱️ wake-step | background oww.reset finished in "
        f"{int((time.monotonic() - _t) * 1000)}ms"
    )


# ---------------------------------------------------------------------------
# Wake-fire decision
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WakeVerdict:
    """The outcome of one wake-fire decision.

    ``should_fire`` is the answer the caller acts on; ``effective_threshold``
    is exposed for structured logging at the fire site.
    """
    should_fire: bool
    effective_threshold: float


def decide_wake_fire(
    *,
    score: float,
    threshold: float,
    now_mono: float,
) -> WakeVerdict:
    """Run the two-gate wake-fire pipeline (score → debounce). See module docstring.

    Side effect: on a fire-through verdict, advances
    ``voice_filters._wake_min_next_ts`` by ``_WAKE_DEBOUNCE_SEC`` under
    ``voice_filters._wake_gate_lock``. A suppressed fire MUST NOT
    advance the gate — that would produce unbounded cooldown extension
    under repeated noise hits.
    """
    if score > 0.05:
        # DEBUG, not INFO: this fires whenever oww hears any speech-like
        # audio — ~12×/s during active windows. At INFO it floods
        # jarvis-log-client's bounded (10k, drop-on-full) network queue and
        # can evict genuinely useful logs, especially across a multi-node
        # demo. Still invaluable for "I said hey jarvis but it didn't hear"
        # triage — just enable debug logging on the node to see it.
        logger.debug(
            "oww-score",
            score=round(float(score), 3),
            threshold=threshold,
        )

    fire_wake = score > threshold

    if fire_wake:
        with voice_filters._wake_gate_lock:
            cooldown_remaining = voice_filters._wake_min_next_ts - now_mono
            override = voice_filters._wake_gate_override_threshold
            if cooldown_remaining > 0 and override is not None and score >= override:
                # Soft gate (not_for_me cool-down): a decisive wake score
                # punches through — the user is clearly addressing us, so
                # the cool-down must not lock them out. Disarm the
                # cool-down and treat as a normal fire.
                logger.info(
                    "wake-override-cooldown",
                    score=round(float(score), 3),
                    override_threshold=override,
                    cooldown_remaining_sec=round(cooldown_remaining, 2),
                )
                voice_filters._wake_gate_override_threshold = None
                voice_filters._wake_min_next_ts = now_mono + _WAKE_DEBOUNCE_SEC
            elif cooldown_remaining > 0:
                fire_wake = False
                if score > 0.3:
                    logger.info(
                        "wake-suppressed-gate",
                        score=round(float(score), 3),
                        cooldown_remaining_sec=round(cooldown_remaining, 2),
                        soft_override_threshold=override,
                    )
            else:
                # Gate expired — a fresh fire re-arms only the hard
                # same-utterance debounce; any stale soft override is
                # cleared with it.
                voice_filters._wake_gate_override_threshold = None
                voice_filters._wake_min_next_ts = (
                    now_mono + _WAKE_DEBOUNCE_SEC
                )

    return WakeVerdict(
        should_fire=fire_wake,
        effective_threshold=threshold,
    )
