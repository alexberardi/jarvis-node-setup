"""Wake-fire decision pipeline — thresholds, gates, and debounce.

The wake-fire pipeline is the spine of every voice command. Three gates
in strict order:

  1. **Score gate** — ``oww.predict()`` must clear ``effective_threshold``,
     where ``effective_threshold`` is the music threshold when playback
     is detected (lower; ~0.12) and the configured static / auto-
     calibrated threshold otherwise (~0.40).

  2. **Music-bleed gate** — when music is playing, raw RMS must spike
     above a running baseline by ``music_energy_multiplier`` (default
     1.5×). This stops the lowered music threshold from tripping on
     speaker bleed alone — OWW regularly scores 0.10-0.18 against
     ambient music even with nobody speaking. Two exemptions:

       * AEC is strongly cancelling (post-AEC score is already
         trustworthy; gate becomes redundant).
       * The score is above the wake_music_trust_score (default 0.95).
         At that confidence we defer to the model — the "user yelling
         Hey Jarvis to stop their own music" case where the gate would
         otherwise trap them. Logged scores up to 0.999 were suppressed
         in the wild on 2026-06-03; user had to power-cycle the node.

     With insufficient RMS history (<6 samples) we conservatively don't
     fire on the lowered music threshold at all — without a baseline
     we can't distinguish music-only from voice-on-music.

  3. **Debounce gate** — atomic under ``voice_filters._wake_gate_lock``.
     If ``_wake_min_next_ts`` is in the future, suppress (probably the
     same OWW utterance double-firing on consecutive 80 ms chunks). If
     in the past, advance the gate by ``_WAKE_DEBOUNCE_SEC`` and let
     the fire through.

The previous, much longer ``not_for_me`` cool-down gate that armed
after a CC misclassification is gone — see the voice_filters module
docstring for the rationale.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass

from jarvis_log_client import JarvisLogger

from core import voice_filters
from core.barge_in import oww_lock as _oww_lock
from core.music_control import is_playing as music_is_playing
from core.voice_filters import _WAKE_DEBOUNCE_SEC
from core.wake_calibration import auto_calibrated_wake_threshold
from utils.config_service import Config


logger = JarvisLogger(service="jarvis-node")


# ---------------------------------------------------------------------------
# Threshold + oww-reset helpers
# ---------------------------------------------------------------------------


def current_wake_threshold() -> float:
    """Wake-word detection threshold, lowered while music is playing.

    Music coming out of the same speaker the mic is hearing reduces the
    model's confidence in "hey jarvis" — without AEC, the user's voice is
    competing with their own playback in the mic stream. Drop the threshold
    aggressively in that case (mirrors barge_in's 0.07) and pair it with
    the energy gate in :func:`decide_wake_fire` so music alone can't
    trigger a wake even though it scores 0.12-0.18 fairly often.

    Was 0.25 before the May-2026 beta: 0.25 was almost never crossed
    against typical music + voice combinations on the Pi Zero mic, so
    "stop the music" verbally was unreliable. The 0.12 floor + energy
    gate together produce both higher recall AND lower false-positive
    rate than either alone.

    When ``wake_word_threshold_auto`` is enabled, the non-music threshold
    is derived from observed wake scores via ``auto_calibrated_wake_threshold``
    — per-node, per-mic, per-room. Default off so existing nodes' static
    threshold isn't silently replaced.
    """
    if music_is_playing():
        return Config.get_float("wake_word_threshold_music", 0.12)
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

    ``should_fire`` is the answer the caller acts on. The other two
    fields are exposed because downstream code needs them (the music-
    mode gate downstream consumes ``effective_music_mode``; structured
    logging consumes both).
    """
    should_fire: bool
    effective_threshold: float
    effective_music_mode: bool


def decide_wake_fire(
    *,
    score: float,
    rms: float,
    pre_wake_rms_values: deque[float],
    music_mode: bool,
    aec_pipeline,
    static_wake_threshold: float,
    music_energy_multiplier: float,
    now_mono: float,
) -> WakeVerdict:
    """Run the three-gate wake-fire pipeline. See module docstring.

    Side effect: on a fire-through verdict, advances
    ``voice_filters._wake_min_next_ts`` by ``_WAKE_DEBOUNCE_SEC`` under
    ``voice_filters._wake_gate_lock``. A suppressed fire MUST NOT
    advance the gate — that would produce unbounded cooldown extension
    under repeated noise hits.
    """
    # Per-frame re-check of music_mode: the outer-loop value goes
    # stale the moment playback starts/stops mid-iteration. AEC's
    # ref_rms window tracks the speaker sink in real time; if it sees
    # signal NOW, flip into music_mode and use the lower music threshold
    # even though the outer-loop value was False. This is what unsticks
    # "music kept playing after a failed stop command — and now I can't
    # wake it".
    effective_music_mode = music_mode or (
        aec_pipeline is not None
        and aec_pipeline.has_recent_reference_signal()
    )
    effective_threshold = (
        Config.get_float("wake_word_threshold_music", 0.12)
        if effective_music_mode
        else static_wake_threshold
    )
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
            threshold=effective_threshold,
            music_mode=effective_music_mode,
        )

    fire_wake = score > effective_threshold

    if fire_wake and effective_music_mode:
        # If AEC is consistently cancelling well, the post-cancellation
        # OWW score is the right signal to trust — the energy gate
        # (designed pre-AEC) becomes redundant and just adds a failure
        # mode. The gate stays in place whenever AEC is weak or
        # unavailable (no playback path, low suppression, adaptation
        # still ramping).
        aec_trusted = (
            aec_pipeline is not None
            and aec_pipeline.is_strongly_active(
                threshold_db=Config.get_float("aec_trust_threshold_db", 5.0),
            )
        )
        if aec_trusted:
            if score > 0.4:
                logger.info(
                    "wake-aec-trusted-skip-gate",
                    score=round(float(score), 3),
                    suppression_db=round(
                        aec_pipeline.recent_suppression_db() or 0.0, 1,
                    ),
                )
        elif len(pre_wake_rms_values) >= 6:
            # Music-mode energy gate: require current RMS to spike above
            # the running baseline (voice ON TOP of music). Without
            # this, the lowered music threshold (~0.12) trips on music
            # alone — the OWW model regularly hits 0.10-0.18 against
            # speaker bleed even when nobody's speaking. Mirrors
            # barge_in's two-tier (low OWW + energy above baseline)
            # approach.
            #
            # EXCEPT when the OWW score is overwhelmingly high (default
            # 0.95+). At that confidence the model is essentially
            # declaring "this IS the wake phrase" — defer to it instead
            # of the gate, because otherwise the user yelling "Hey
            # Jarvis" to stop their own music gets trapped (logged
            # scores up to 0.999 suppressed in the wild on 2026-06-03;
            # user had to power-cycle the node). False positives at
            # that confidence level are vanishingly rare even against
            # music bleed.
            sorted_rms = sorted(pre_wake_rms_values)
            baseline_rms = sorted_rms[len(sorted_rms) // 2]
            energy_floor = baseline_rms * music_energy_multiplier
            trust_score = Config.get_float("wake_music_trust_score", 0.95)
            if score >= trust_score:
                logger.info(
                    "wake-music-trust-score-bypass",
                    score=round(float(score), 3),
                    rms=round(rms, 1),
                    baseline_rms=round(baseline_rms, 1),
                    energy_floor=round(energy_floor, 1),
                    trust_score=trust_score,
                )
            elif rms <= energy_floor:
                fire_wake = False
                if score > 0.08:
                    logger.info(
                        "wake-suppressed-music-bleed",
                        score=round(float(score), 3),
                        rms=round(rms, 1),
                        baseline_rms=round(baseline_rms, 1),
                        energy_floor=round(energy_floor, 1),
                    )
        else:
            # Not enough history yet — be conservative and don't fire
            # on the music-mode threshold until we've sampled ~480 ms
            # of baseline.
            fire_wake = False

    if fire_wake:
        with voice_filters._wake_gate_lock:
            cooldown_remaining = voice_filters._wake_min_next_ts - now_mono
            if cooldown_remaining > 0:
                fire_wake = False
                if score > 0.3:
                    logger.info(
                        "wake-suppressed-gate",
                        score=round(float(score), 3),
                        cooldown_remaining_sec=round(cooldown_remaining, 2),
                    )
            else:
                voice_filters._wake_min_next_ts = (
                    now_mono + _WAKE_DEBOUNCE_SEC
                )

    return WakeVerdict(
        should_fire=fire_wake,
        effective_threshold=effective_threshold,
        effective_music_mode=effective_music_mode,
    )
