"""Wake-fire decision pipeline — threshold and debounce.

The wake-fire pipeline is the spine of every voice command. Two gates
in strict order:

  1. **Score gate** — ``oww.predict()`` must clear the configured static
     / auto-calibrated threshold (~0.40) — or, during self-playback, the
     music-profile threshold ``wake_word_threshold_music`` (default 0.30;
     see :func:`current_wake_threshold_with_profile`). The music profile
     is a PURE threshold swap keyed off the cached self-playing flag —
     nothing else. It is explicitly NOT the June-2026 energy gate reborn:
     that machinery (the ~0.12 lowered threshold + RMS-spike-over-baseline
     check + ``wake_music_trust_score`` bypass) was retired because it
     actively discarded real wakes (PRD ``prds/wake-during-music.md``:
     18 ``wake-suppressed-music-bleed`` events vs 4 fires on the prod
     kitchen node). 0.30 needs no compensating energy gate: speaker bleed
     scores 0.10–0.18, comfortably below it, while loud music caps real
     wakes at 0.14–0.43 — 0.30 recovers the top of that band. CC clip
     verification absorbs the extra false fires (fail-open doctrine).

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
from core.music_control import is_self_playing
from core.voice_filters import _WAKE_DEBOUNCE_SEC
from core.wake_calibration import auto_calibrated_wake_threshold
from utils.config_service import Config


logger = JarvisLogger(service="jarvis-node")


# ---------------------------------------------------------------------------
# Threshold + oww-reset helpers
# ---------------------------------------------------------------------------


def current_wake_threshold() -> float:
    """Wake-word detection threshold (float-only compat wrapper).

    See :func:`current_wake_threshold_with_profile` for the full
    semantics — this wrapper exists for call sites that only need the
    number (status prints in voice_listener).
    """
    threshold, _profile = current_wake_threshold_with_profile()
    return threshold


def current_wake_threshold_with_profile() -> tuple[float, str]:
    """Wake-word detection threshold + which profile produced it.

    THE single choke point for the wake threshold. Two profiles:

    * ``"normal"`` — the static ``wake_word_threshold`` (~0.40), or the
      auto-calibrated value when ``wake_word_threshold_auto`` is on.

    * ``"music"`` — during self-playback (``is_self_playing()``, the
      cached flag — no pactl round-trip here), the threshold drops to
      ``wake_word_threshold_music`` (default 0.30). Loud-music physics
      (June-2026 data): OWW caps at 0.14–0.43 while music plays, so the
      normal 0.40 discards most real wakes; speaker-bleed-only scores
      sit at 0.10–0.18. 0.30 sits above the bleed band and harvests the
      recoverable top of the cap band. This is a pure threshold swap —
      deliberately NO RMS/energy checks and NO trust scores (the sins
      of the retired June energy gate; see module docstring). Extra
      false fires this admits are absorbed by CC verification (clip
      phrase-match), which is the fail-open backstop. Set
      ``wake_word_threshold_music`` <= 0 to disable the profile and use
      the normal threshold during music.

    The profile string is tagged onto the 'Wake fired' structured log
    (``threshold_profile``) so music-profile fires can be peeled apart
    from normal fires in Loki per-fire telemetry.
    """
    static_default = Config.get_float("wake_word_threshold", 0.4)
    if Config.get_bool("wake_word_threshold_auto", False):
        normal = auto_calibrated_wake_threshold(static_default)
    else:
        normal = static_default
    if is_self_playing():
        music = Config.get_float("wake_word_threshold_music", 0.30)
        if music > 0:
            return music, "music"
    return normal, "normal"


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
