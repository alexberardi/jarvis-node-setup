"""Main wake-word detection loop.

Owns the long-running ``while True`` that drives every wake cycle on
the node: outer iteration setup (threshold/music snapshot, deques),
inner per-chunk scoring (resample → AEC → openWakeWord predict →
``decide_wake_fire``), the alert-drain break-out path, and the
post-fire orchestration chain (``bus.unsubscribe`` → ``oww.reset``
in the background → ``pause_active_playback`` → wake-audio capture
→ ``handle_keyword_detected`` → warmup thread → ``listen`` →
processing ack → ``send_for_transcription`` → ``follow_up_loop``).

This module is the orchestrator of the modules that came out of the
earlier refactor phases — it owns no logic of its own beyond glue
and the per-iteration state (deques, score snapshot, RMS stats,
adaptive silence threshold). Every decision lives in a leaf module:

  * ``core.wake_detector`` — the three-gate fire verdict
  * ``core.wake_response`` — TTS ack + LED + warmup pre-cache
  * ``core.wake_transcription`` — STT round-trip + CC orchestration
  * ``core.follow_up_loop`` — post-TTS conversation continuation
  * ``core.alert_announcer`` — high-priority alert TTS during quiet
  * ``core.music_control`` — ducking + is_playing
  * ``core.vad_thresholds`` — barge-in + adaptive silence
  * ``core.wake_calibration`` — auto-tune via legitimate-score history

Cross-cutting runtime deps (the bounded executor for fire-and-forget
background work, and the ``_wake_paused`` Event consulted by the
inner loop) are injected via :func:`set_runtime` from
``scripts.voice_listener`` at module init.
"""

from __future__ import annotations

import queue
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import numpy as np

from jarvis_log_client import JarvisLogger

from core.alert_announcer import (
    drain_alert_announcements,
    has_pending_high_priority_alerts,
)
from core.audio_bus import AudioBus
from core.barge_in import BargeInMonitor, oww_lock as _oww_lock
from core.follow_up_loop import follow_up_loop
from core.music_control import (
    is_playing as music_is_playing,
    pause_active_playback,
    resume_active_playback,
    wake_music_energy_multiplier,
)
from core.platform_audio import platform_audio
from core.vad_thresholds import (
    adaptive_silence_threshold as _adaptive_silence_threshold,
    barge_in_enabled,
    barge_in_energy_threshold,
    barge_in_threshold,
    pre_wake_vad_threshold as _pre_wake_vad_threshold,
)
from core.wake_calibration import record_legitimate_wake_score
from core.wake_detector import (
    current_wake_threshold,
    decide_wake_fire,
    locked_oww_reset,
)
from core.wake_response import (
    fetch_next_processing_ack,
    handle_keyword_detected,
    play_processing_ack,
    run_warmup,
    set_led_transient,
)
from core.wake_transcription import (
    get_last_speaker,
    send_for_transcription,
    try_capture_wake_audio,
)
from scripts.speech_to_text import listen
from utils.config_service import Config

if TYPE_CHECKING:
    from openwakeword.model import Model as OWWModel
    from utils.command_execution_service import CommandExecutionService
    from core.aec_pipeline import AecPipeline


logger = JarvisLogger(service="jarvis-node")


# openWakeWord requires 16 kHz audio in 1280-sample (80 ms) chunks.
OWW_RATE = 16000
OWW_CHUNK = 1280

# One captured chunk = one OWW frame = 80 ms of audio.
_CHUNK_SECONDS: float = OWW_CHUNK / OWW_RATE  # 0.08

# Pre-wake VAD ring buffer: ~5 s of "was-speech" frames before each
# wake event. Reported to CC as a direction hint for not_for_me.
PRE_WAKE_VAD_WINDOW_SECS: float = 5.0
PRE_WAKE_VAD_FRAMES: int = max(1, int(PRE_WAKE_VAD_WINDOW_SECS / _CHUNK_SECONDS))

# How many inner-loop iterations between alert-queue polls. At 0.5 s
# of queue.get timeout per empty pull, this is ~5 s wall-clock. Module
# constant so tests can shorten it.
_ALERT_CHECK_INTERVAL: int = 60


# scipy.signal is lazy-imported on the first audio chunk — see the
# note at the top of scripts/voice_listener.py.
_resample_poly = None


def _get_resample_poly():
    """Lazy-import scipy.signal.resample_poly on first audio chunk."""
    global _resample_poly
    if _resample_poly is None:
        from scipy.signal import resample_poly  # noqa: E402
        _resample_poly = resample_poly
    return _resample_poly


# Runtime deps injected from voice_listener at module init.
_bg_executor: ThreadPoolExecutor | None = None
_wake_paused: threading.Event | None = None


def set_runtime(
    *,
    bg_executor: ThreadPoolExecutor,
    wake_paused: threading.Event,
) -> None:
    """Inject runtime deps from voice_listener.

    Called once at voice_listener module init. Tests don't call this —
    they monkeypatch ``wake_loop._bg_executor`` and ``wake_loop._wake_paused``
    directly.
    """
    global _bg_executor, _wake_paused
    _bg_executor = bg_executor
    _wake_paused = wake_paused


def run_wake_loop(
    bus: AudioBus,
    oww: "OWWModel",
    command_service: "CommandExecutionService",
    stt_provider,
    validation_handler,
    aec_pipeline: "AecPipeline | None",
    wake_word_model: str,
) -> None:
    """Run the wake loop until KeyboardInterrupt.

    Resources (``bus``, ``oww``, ``aec_pipeline``) are owned by the
    caller (:func:`scripts.voice_listener.start_voice_listener`),
    which sets them up before calling and tears them down after.

    The loop body has been unchanged across the refactor — every
    inner step delegates to a leaf module. The only state owned here
    is per-iteration: the deques, the wake-fire snapshot score, the
    adaptive silence threshold, and the conversation IDs.
    """
    resample_down = bus.rate // OWW_RATE  # 3 for 48 kHz → 16 kHz
    alert_check_counter = 0

    while True:
        # Safety net: ensure no stale cancel state from a prior
        # barge-in prevents wake-response or other audio.
        platform_audio.reset_cancel()

        # Re-read the wake threshold each outer iteration so mobile-app
        # updates apply without a service restart. Using a local also
        # keeps the inner loop hot path off the disk.
        wake_threshold = current_wake_threshold()
        # Snapshot music state alongside the threshold so the energy
        # gate below matches the threshold's assumption. music_is_playing
        # round-trips to pactl (~ms) so we don't want to call it per
        # chunk; we only need it to be coherent with the threshold
        # used for the current outer iteration.
        #
        # Secondary check via AEC's reference reader: pactl detection
        # is fragile (misses stuck playback after a failed stop-music
        # command, briefly-corked sink-inputs, unrecognized player
        # binaries). ref_rms reflects what's actually being sent to
        # the speaker, so it catches the cases pactl misses.
        music_mode = music_is_playing() or (
            aec_pipeline is not None
            and aec_pipeline.has_recent_reference_signal()
        )
        music_energy_multiplier = wake_music_energy_multiplier()

        wake_q = bus.subscribe("wake")
        score = 0.0
        # Pre-wake VAD ring buffer — one bool per 80 ms chunk, last
        # PRE_WAKE_VAD_FRAMES kept. On wake fire we sum it and report
        # how many seconds of speech happened in the window before wake.
        # Fresh per outer iteration so prior interactions don't leak in.
        pre_wake_speech_frames: deque[bool] = deque(maxlen=PRE_WAKE_VAD_FRAMES)
        # Parallel RMS-value deque used only for the wake-fire diagnostic
        # log — lets us see the actual mic baseline so the threshold can
        # be tuned per room without guessing.
        pre_wake_rms_values: deque[float] = deque(maxlen=PRE_WAKE_VAD_FRAMES)
        pre_wake_vad_threshold: float = _pre_wake_vad_threshold()
        pre_wake_speech_seconds: float | None = None
        try:
            was_paused = False
            while True:
                alert_check_counter += 1
                if alert_check_counter >= _ALERT_CHECK_INTERVAL:
                    alert_check_counter = 0
                    if has_pending_high_priority_alerts():
                        # Let the alert drain run — it uses the bus too.
                        break  # ← exits inner loop with score==0; see below

                try:
                    raw_data = wake_q.get(timeout=0.5)
                except queue.Empty:
                    continue

                # While paused, drop the chunk and don't score it. The
                # queue still drains so we don't process stale audio
                # the moment we resume.
                if _wake_paused is not None and _wake_paused.is_set():
                    was_paused = True
                    continue

                # First chunk after a pause: reset the openWakeWord LSTM
                # state. Without this, residual context from before the
                # pause (often the wake response audio echoing back)
                # immediately re-triggers a wake event. Also drop the
                # pre-wake VAD buffer — frames from before the pause
                # are no longer "the room before this wake".
                if was_paused:
                    with _oww_lock:
                        oww.reset()
                    pre_wake_speech_frames.clear()
                    pre_wake_rms_values.clear()
                    was_paused = False

                samples = np.frombuffer(raw_data, dtype=np.int16)

                # Per-chunk RMS for the pre-wake VAD ring buffer. Use
                # the raw 48 kHz samples (before resample/clip) so the
                # energy reading is unmodified mic input. Tiny cost.
                rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
                pre_wake_speech_frames.append(rms > pre_wake_vad_threshold)
                pre_wake_rms_values.append(rms)

                if resample_down > 1:
                    resampled = _get_resample_poly()(samples, up=1, down=resample_down)
                    samples = np.clip(resampled, -32768, 32767).astype(np.int16)

                if aec_pipeline is not None:
                    samples = aec_pipeline.process(samples)
                    # The pre-wake RMS readings (used by the music
                    # energy gate below) were taken from the raw mic
                    # above — that signal is still dominated by music
                    # bleed even when AEC has cleaned the voice into
                    # a strong OWW score. Re-measure on the cleaned
                    # signal so the gate's baseline and current-frame
                    # RMS are both post-cancellation and comparable.
                    # Without this, AEC-cleaned wakes score 0.5-0.8
                    # but get suppressed because raw mic bleed > raw
                    # mic-with-voice in RMS terms.
                    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
                    pre_wake_rms_values[-1] = rms

                with _oww_lock:
                    predictions = oww.predict(samples)
                score = predictions.get(wake_word_model, 0)
                # Per-chunk predict timing logging was removed
                # 2026-05-20: on the Pi Zero, logger.info() costs
                # 5-20 ms per call, and our >60 ms warning was
                # firing every chunk in steady state — creating a
                # feedback loop where the logging itself pushed
                # predict from 60 ms toward 80 ms, then logged
                # again. We measured predict at 60-80 ms (right at
                # the 80 ms real-time budget) — that's a known
                # property of openWakeWord on this hardware, not
                # something logging per chunk can help with.
                # The three-gate wake-fire pipeline (score >
                # threshold + music-bleed gate + same-utterance
                # debounce) lives in ``core/wake_detector.py``. On
                # a fired verdict it advances
                # ``voice_filters._wake_min_next_ts`` under the
                # gate lock so we don't double-fire on the next
                # 80 ms chunk of the same OWW utterance.
                now_mono = time.monotonic()
                verdict = decide_wake_fire(
                    score=score,
                    rms=rms,
                    pre_wake_rms_values=pre_wake_rms_values,
                    music_mode=music_mode,
                    aec_pipeline=aec_pipeline,
                    static_wake_threshold=wake_threshold,
                    music_energy_multiplier=music_energy_multiplier,
                    now_mono=now_mono,
                )
                fire_wake = verdict.should_fire
                if fire_wake:
                    t_wake_fired = now_mono
                    # Snapshot the score for the auto-calibrator. The
                    # ``score`` variable will be overwritten when the
                    # outer loop iterates again; we need it later
                    # (after CC's not_for_me verdict) to decide whether
                    # this wake counts as a "legitimate" data point.
                    score_at_wake = float(score)
                    speech_frames = sum(pre_wake_speech_frames)
                    pre_wake_speech_seconds = round(
                        speech_frames * _CHUNK_SECONDS, 2,
                    )
                    # Diagnostic RMS stats for threshold calibration.
                    if pre_wake_rms_values:
                        rms_sorted = sorted(pre_wake_rms_values)
                        rms_n = len(rms_sorted)
                        rms_stats = {
                            "max": round(rms_sorted[-1], 1),
                            "p95": round(rms_sorted[int(rms_n * 0.95)], 1),
                            "p75": round(rms_sorted[int(rms_n * 0.75)], 1),
                            "median": round(rms_sorted[rms_n // 2], 1),
                            "min": round(rms_sorted[0], 1),
                        }
                    else:
                        rms_stats = {}
                    logger.info(
                        "Wake fired",
                        score=round(float(score), 3),
                        pre_wake_speech_seconds=pre_wake_speech_seconds,
                        pre_wake_window_seconds=PRE_WAKE_VAD_WINDOW_SECS,
                        buffered_frames=len(pre_wake_speech_frames),
                        vad_threshold=pre_wake_vad_threshold,
                        rms_stats=rms_stats,
                    )
                    # Lock the per-cycle silence threshold to the ambient
                    # noise floor observed RIGHT before wake. Used below
                    # for the command listen() (and follow-up listens),
                    # so a static config value can't be wrong for the
                    # current room state.
                    adaptive_silence_threshold = _adaptive_silence_threshold(rms_stats)
                    if adaptive_silence_threshold is not None:
                        logger.info(
                            "Adaptive silence threshold",
                            silence_threshold=adaptive_silence_threshold,
                            ambient_median_rms=rms_stats.get("median"),
                        )
                    break
        finally:
            _t_unsub_start = time.monotonic()
            bus.unsubscribe("wake")
            logger.info(
                f"⏱️ wake-step | bus.unsubscribe took "
                f"{int((time.monotonic() - _t_unsub_start) * 1000)}ms"
            )

        # If we broke out without a wake (alert-drain case), handle
        # alerts and loop.
        if score <= wake_threshold:
            try:
                drain_alert_announcements(
                    bus, command_service, stt_provider, validation_handler,
                )
            except Exception as e:
                logger.warning("Alert drain failed", error=str(e))
            with _oww_lock:
                oww.reset()
            print(f"Ready — say '{wake_word_model.replace('_', ' ')}'")
            continue

        # Clear oww's LSTM state so the next wake cycle doesn't
        # false-retrigger on the tail of the wake word we just
        # detected — BUT do it in the background. On the Pi Zero
        # ``oww.reset()`` is ~1.5 s of model state reinit, and
        # blocking on it here pushes "You rang?" 1.5 s later for
        # zero user benefit (the next wake loop won't run for
        # multiple seconds while we play TTS, listen, etc.). The
        # _oww_lock guarantees the reset finishes before any future
        # predict() runs.
        _t_reset_start = time.monotonic()
        if _bg_executor is not None:
            _bg_executor.submit(locked_oww_reset, oww)
        logger.info(
            f"⏱️ wake-step | oww.reset SUBMITTED to background in "
            f"{int((time.monotonic() - _t_reset_start) * 1000)}ms"
        )

        # Drop music volume for the duration of the command exchange so
        # the user's voice isn't competing with their own playback.
        # Fire-and-forget background — see note above the executor.
        _t_duck_start = time.monotonic()
        if _bg_executor is not None:
            _bg_executor.submit(pause_active_playback)
        logger.info(
            f"⏱️ wake-step | duck submit took "
            f"{int((time.monotonic() - _t_duck_start) * 1000)}ms"
        )

        # Snapshot the wake-word audio from the bus *now* — before
        # handle_keyword_detected plays the TTS ack which can take
        # 500-1500ms. The bus only holds ~2s of history so any later
        # snapshot risks falling outside that window. The same
        # snapshot is reused for every follow-up in this conversation.
        wake_audio_path = try_capture_wake_audio(bus)

        # Initialize ``result`` up-front so the outer ``finally``'s
        # ``result.get("on_response_complete")`` is always safe — without
        # this, a KeyboardInterrupt or other propagation through
        # handle_keyword_detected / warmup_thread.start() / barge-in
        # setup hits the finally before ``result = None`` runs and
        # raises UnboundLocalError, masking the original exception.
        result: dict | None = None

        try:
            _t_handle_start = time.monotonic()
            logger.info(
                f"⏱️ wake-step | T+"
                f"{int((_t_handle_start - t_wake_fired) * 1000)}ms "
                f"entering handle_keyword_detected"
            )
            try:
                handle_keyword_detected()
            except Exception as e:
                logger.warning("Wake response TTS failed, continuing", error=str(e))
            _t_handle_end = time.monotonic()
            logger.info(
                f"⏱️ wake-step | handle_keyword_detected took "
                f"{int((_t_handle_end - _t_handle_start) * 1000)}ms "
                f"(T+{int((_t_handle_end - t_wake_fired) * 1000)}ms total)"
            )

            conversation_id = str(uuid.uuid4())
            warmup_result: dict = {"success": False}
            warmup_thread = threading.Thread(
                target=run_warmup,
                args=(command_service, conversation_id, *get_last_speaker(), warmup_result),
                daemon=True,
            )
            warmup_thread.start()

            barge_in: BargeInMonitor | None = None
            if barge_in_enabled():
                barge_in = BargeInMonitor(
                    bus, oww, wake_word_model,
                    threshold=barge_in_threshold(),
                    energy_threshold=barge_in_energy_threshold(),
                )

            _t_listen_start = time.monotonic()
            try:
                # history_secs=0 + adaptive quiet-wait (v0.1.65) inside
                # listen(): do NOT replay the wake-response TTS tail
                # (that bug made the node transcribe and respond to
                # itself). Quiet-wait drains the speaker tail per-room.
                #
                # silence_threshold passed explicitly when adaptive mode
                # produced one — calibrated to the room's ambient floor
                # at the moment of wake (see adaptive_silence_threshold).
                # When None, listen() falls back to its static config
                # value, preserving prior behavior.
                #
                # quiet_wait gated on actual speaker activity: with
                # wake_ack_audio_enabled=False AND no music playing,
                # the speaker is silent — running the drain would
                # misidentify the user's own command (4000+ RMS) as
                # speaker echo and discard the first 400 ms of speech.
                # That's the "cut off mid-sentence" pattern from prod.
                # AEC reference-signal is the right signal: True only
                # when the speaker actually had recent output.
                speaker_recently_active = (
                    aec_pipeline is not None
                    and aec_pipeline.has_recent_reference_signal()
                )
                listen_quiet_wait_secs = None if speaker_recently_active else 0.0
                # silence_duration override (default 0.8 s): natural
                # mid-sentence pauses (between clauses, taking a breath)
                # commonly run 250-400 ms. The historical 0.3 s default
                # was chopping multi-clause commands. 0.8 s tolerates a
                # normal pause without dragging out silence detection on
                # actual end-of-utterance.
                listen_silence_duration = Config.get_float(
                    "listen_silence_duration_secs", 0.8,
                )
                recording = listen(
                    bus, history_secs=0.0,
                    silence_threshold=adaptive_silence_threshold,
                    silence_duration=listen_silence_duration,
                    quiet_wait_secs=listen_quiet_wait_secs,
                )
                _t_listen_end = time.monotonic()
                logger.info(
                    f"⏱️ wake-step | listen() took "
                    f"{int((_t_listen_end - _t_listen_start) * 1000)}ms "
                    f"(T+{int((_t_listen_end - t_wake_fired) * 1000)}ms total)"
                )

                # Suppress the post-listen processing-ack when audio
                # acks are disabled — the deferred wake-ack (played by
                # on_llm_fallback inside process_voice_command) covers
                # the same job without stacking two back-to-back acks.
                if Config.get_bool("wake_ack_audio_enabled", True):
                    _t_ack_start = time.monotonic()
                    ack_played = play_processing_ack()
                    logger.info(
                        f"⏱️ wake-step | processing-ack took "
                        f"{int((time.monotonic() - _t_ack_start) * 1000)}ms "
                        f"(played={ack_played})"
                    )
                else:
                    ack_played = False

                if barge_in:
                    barge_in.start()

                _t_xform_start = time.monotonic()
                result = send_for_transcription(
                    recording, command_service, stt_provider, validation_handler,
                    warmup_thread=warmup_thread,
                    conversation_id=conversation_id,
                    warmup_result=warmup_result,
                    skip_ack=ack_played,
                    pre_wake_speech_seconds=pre_wake_speech_seconds,
                    wake_audio_path=wake_audio_path,
                )
                # Feed the auto-calibrator: wake scores that produced a
                # real interaction (not the not_for_me silent abort)
                # become anchors for the threshold. Failures are NOT
                # recorded, biasing the calibrator toward conservatism
                # — a wake that CC rejected might have been a real
                # false-positive and including it would lower the bar
                # for genuine false positives.
                #
                # not_for_me used to arm a multi-second cooldown gate
                # here. Removed: a probabilistic verdict shouldn't lock
                # the room out. TTS is already skipped upstream; the
                # next wake fires normally.
                if isinstance(result, dict) and not result.get("not_for_me"):
                    record_legitimate_wake_score(score_at_wake)
                # Capture TTS-end time RIGHT after send_for_transcription
                # returns (which is right after speak_result completes).
                # The follow-up loop uses this to know how far back to look
                # in the bus history for speech the user uttered before the
                # listener subscribed. barge_in.stop() in the finally below
                # routinely costs ~1.5s — that 1.5s is exactly the gap users
                # speak into.
                tts_end_ts = time.monotonic()
                _t_xform_end = tts_end_ts
                logger.info(
                    f"⏱️ wake-step | send_for_transcription took "
                    f"{int((_t_xform_end - _t_xform_start) * 1000)}ms "
                    f"(T+{int((_t_xform_end - t_wake_fired) * 1000)}ms total "
                    f"— incl STT + CC + TTS playback)"
                )
            except Exception as e:
                logger.warning("Command processing failed, resuming listener", error=str(e))
                print(f"Command failed: {e}")
                tts_end_ts = time.monotonic()
            finally:
                _t_barge_stop_start = time.monotonic()
                if barge_in:
                    barge_in.stop()
                logger.info(
                    f"⏱️ wake-step | barge_in.stop took "
                    f"{int((time.monotonic() - _t_barge_stop_start) * 1000)}ms"
                )

            if barge_in and barge_in.was_interrupted:
                logger.info("Barge-in: TTS interrupted, returning to wake word")
                platform_audio.reset_cancel()
                # Clear the LED transient overlay so the cyan "speaking"
                # pattern that was active during TTS doesn't linger after
                # the user cut us off — without this the LED stays at
                # whatever state TTS left it in until the next wake.
                set_led_transient(None)
                # Don't try to capture a new command here — the user
                # interrupted to STOP the response.  They'll say the
                # wake word again when they're ready.
            else:
                try:
                    follow_up_loop(
                        bus, result, command_service, stt_provider, validation_handler,
                        wake_word_model=wake_word_model,
                        oww=oww, tts_end_ts=tts_end_ts,
                        wake_audio_path=wake_audio_path,
                        silence_threshold=adaptive_silence_threshold,
                        silence_duration=listen_silence_duration,
                    )
                except Exception as e:
                    logger.warning("Follow-up loop error, resuming wake word", error=str(e))
        finally:
            # Mirror the duck: always background, no pre-check. The
            # restore (pactl move-back + SIGCONT) is idempotent against
            # missing/unmoved targets, so spending background CPU when
            # nothing was paused is harmless. Critical: never block
            # the return to wake-word mode on pactl round-trips.
            #
            # If the executed command provided an on_response_complete
            # callback (media commands deferring their actual play() so
            # the first seconds aren't lost to the duck null sink), run
            # it AFTER the restore so the sink-input is back on the real
            # ALSA sink by the time librespot/MA/mpv start streaming.
            _on_complete = (
                result.get("on_response_complete")
                if isinstance(result, dict) else None
            )

            def _restore_then_complete(
                on_complete=_on_complete,
            ) -> None:
                resume_active_playback()
                # The TLV320 driver can't resume from SUSPENDED on this
                # hardware — pulse logs "Resume failed, couldn't
                # restore original sample settings" the moment a new
                # sink-input arrives. Between TTS aplay exiting and
                # the deferred-play hook firing, the sink frequently
                # transitions IDLE → SUSPENDED in a tiny window where
                # an IDLE check would miss it. Force a fresh alsa-card
                # reload here so the deferred player's sink-input
                # (librespot / MA / mpv) lands on a guaranteed-healthy
                # sink. ~500ms cost, only when on_complete is set.
                if on_complete is not None:
                    try:
                        from utils.audio_volume import (
                            force_reload_alsa_card,
                        )
                        force_reload_alsa_card()
                    except Exception as e:
                        logger.debug(
                            "Pre-deferred-play reload raised",
                            error=str(e),
                        )
                    try:
                        on_complete()
                    except Exception as e:
                        logger.warning(
                            "on_response_complete callback raised",
                            error=str(e),
                        )

            if _bg_executor is not None:
                _bg_executor.submit(_restore_then_complete)
            else:
                _restore_then_complete()
            # Safety net: always clear the transient LED state when
            # returning to wake-word mode, so a half-finished path can't
            # leave the pinwheel (or any other transient) stuck.
            set_led_transient(None)

        logger.info(
            f"⏱️ wake-cycle COMPLETE | total="
            f"{int((time.monotonic() - t_wake_fired) * 1000)}ms "
            f"from wake_fired to return-to-idle"
        )
        if _bg_executor is not None:
            _bg_executor.submit(fetch_next_processing_ack)
        print(f"Ready — say '{wake_word_model.replace('_', ' ')}'")
