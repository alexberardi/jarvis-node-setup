"""Post-TTS follow-up loop — listen for conversation continuation.

When the assistant finishes speaking, the loop listens for a few seconds
of follow-up speech and either continues the conversation or returns to
wake-word mode. THE brittleness epicenter of the wake cycle pre-refactor
— 11 distinct exit paths through four layers of guards plus a barge-in
interrupt, all keyed off mic input the user did or didn't make.

The four guard layers in order:

  1. **Hard cap** (``max_follow_up_iterations``, default 5) — exits
     regardless of audio. Prevents infinite loops on stuck input.
  2. **Decaying timeout** — each iteration's ``listen_for_follow_up``
     waits ``FOLLOW_UP_TIMEOUT_DECAY`` seconds less than the previous
     one, floored at ``FOLLOW_UP_MIN_TIMEOUT``. Real follow-ups happen
     fast; long silences with eventual noise are almost certainly
     ambient.
  3. **Follow-up noise** (:func:`is_follow_up_noise` from
     ``core.conversation_filters``) — short/repeated transcripts that
     slip past ``is_non_speech``. Two consecutive noise hits
     (``MAX_CONSECUTIVE_NOISE``) end the loop.
  4. **Self-echo** (:func:`looks_like_self_echo`) — checks the captured
     transcript against the last assistant reply. Hard exit on a single
     match — one confirmed echo is enough to suppress the phantom
     "Here is sad music" feedback loop.

Plus result-driven exits (``clear_history`` / ``not_for_me`` from CC),
the ``not_for_me`` short-circuit at entry, and barge-in interruption
during the response itself.

The ``not_for_me`` cool-down gate from earlier iterations is removed —
see ``core.voice_filters`` for the rationale.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Callable

from jarvis_log_client import JarvisLogger

from clients.responses.jarvis_command_center import ValidationRequest
from core.audio_bus import AudioBus
from core.barge_in import BargeInMonitor
from core.conversation_filters import (
    extract_assistant_text,
    is_follow_up_noise,
    looks_like_self_echo,
)
from core.music_control import pause_active_playback
from core.platform_audio import platform_audio
from core.vad_thresholds import (
    barge_in_enabled,
    barge_in_energy_threshold,
    barge_in_threshold,
)
from core.voice_filters import is_non_speech
from core.wake_response import set_led_transient
from core.wake_transcription import try_build_speaker_audio
from scripts.speech_to_text import listen_for_follow_up
from utils.config_service import Config

if TYPE_CHECKING:
    from utils.command_execution_service import CommandExecutionService


logger = JarvisLogger(service="jarvis-node")


# Follow-up loop safety limits — prevents ambient noise from keeping
# the conversation alive indefinitely (the "perpetual follow-up" bug).
MAX_FOLLOW_UP_ITERATIONS = 5    # Hard cap on follow-up iterations
MAX_CONSECUTIVE_NOISE = 2       # Exit after N consecutive noise transcriptions
FOLLOW_UP_TIMEOUT_DECAY = 2.0   # Shorten listen window by this per iteration (s)
FOLLOW_UP_MIN_TIMEOUT = 3.0     # Floor for the decayed timeout (s)


# Runtime dep: the bounded ThreadPoolExecutor that owns fire-and-forget
# pre-generation work. Injected from voice_listener at module init via
# :func:`set_runtime` (same pattern as ``core.wake_response``). Default
# None — calls fall through to a synchronous fallback in tests that
# install their own fake executor.
_bg_executor = None


def set_runtime(*, bg_executor) -> None:
    """Inject the runtime deps from voice_listener.

    Called once at voice_listener module init. Tests don't call this —
    they monkeypatch ``follow_up_loop._bg_executor`` directly.
    """
    global _bg_executor
    _bg_executor = bg_executor


def follow_up_loop(
    bus: AudioBus,
    initial_result: dict | None,
    command_service: "CommandExecutionService",
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
    wake_word_model: str,
    oww=None,
    tts_end_ts: float | None = None,
    wake_audio_path: str | None = None,
    silence_threshold: int | None = None,
    silence_duration: float | None = None,
) -> None:
    """Listen for follow-up speech after TTS completes.

    If the user speaks within the follow-up window, process it as a
    continuation of the conversation. Each successful follow-up restarts
    the timer. Silence or error breaks out to wake word mode.

    If ``oww`` is provided, barge-in monitoring is active during TTS
    playback — the wake word interrupts the response and returns to the
    main wake detection loop.
    """
    # Default 4s (was 10s): in beta deployments 10s felt overkill —
    # users either follow up within 2-3s or have moved on entirely, and
    # the long open window leaves the LED in "listening" too long after
    # the response. 4s still covers the typical 2-3s reaction time.
    # Users can override via config.
    follow_up_seconds: float = Config.get_float("follow_up_listen_seconds", 4.0)
    if follow_up_seconds <= 0:
        return

    # If the server signalled not_for_me (ambient false-wake), skip the
    # follow-up window entirely — otherwise we'd sit listening while the
    # user keeps talking to whoever they were actually addressing.
    # Note: only ``not_for_me`` short-circuits — ``clear_history`` alone
    # is set by many normal one-shot commands (timers, lamp toggles) and
    # those should still allow follow-ups.
    if initial_result and initial_result.get("not_for_me"):
        logger.info("Initial result signalled not_for_me, skipping follow-up loop")
        return

    # The model marked its reply terminal (<exchange_complete/> → CC set
    # end_of_exchange). Don't open the window at all — return to idle.
    # A wrong close costs one re-wake; a lingering window costs the room
    # being listened to and answered.
    if initial_result and initial_result.get("end_of_exchange"):
        logger.info("Initial result signalled end_of_exchange, skipping follow-up loop")
        return

    conversation_id: str | None = None
    if initial_result and initial_result.get("success") and not initial_result.get("pre_routed"):
        # A pre-routed initial turn never registered a CC conversation, so we
        # must not "continue" it (CC would 400). Leaving conversation_id=None
        # makes the next follow-up start a fresh CC conversation via
        # process_voice_command — which also carries recently_shown_items over
        # in node_context so "mark those as read" still resolves.
        conversation_id = initial_result.get("conversation_id")

    # The AudioBus ring buffer is already capturing post-TTS audio. Track
    # when TTS ended so each iteration can ask the bus for "everything
    # since then" via history_secs, capping at the bus's 2s ring capacity.
    # Without this, anything the user said in the gap between TTS-end and
    # listener-attach (~1.5s typical, dominated by barge_in.stop drain)
    # was lost. The caller passes its own measurement of "TTS just ended"
    # because by the time we're called the gap may already be ~1.5s old.
    if tts_end_ts is None:
        tts_end_ts = time.monotonic()
    iteration = 0
    max_iterations: int = Config.get_int("max_follow_up_iterations", MAX_FOLLOW_UP_ITERATIONS)
    consecutive_noise: int = 0
    prev_text: str | None = None
    # Track the last thing the assistant said so the echo check can
    # suppress follow-ups that look like the mic capturing the node's
    # own TTS tail. Seeded from the initial result so the first
    # follow-up iteration is already protected.
    last_assistant_text: str = extract_assistant_text(initial_result)

    while True:
        iteration += 1

        # Layer 1: Hard cap — prevents infinite loops regardless of audio.
        if iteration > max_iterations:
            logger.info("Follow-up max iterations reached, returning to wake word mode",
                        max_iterations=max_iterations)
            break

        # Layer 2: Decaying timeout — later iterations wait less for onset.
        # Real follow-ups happen quickly; long silences with eventual noise
        # are almost certainly ambient.
        iter_timeout = max(
            FOLLOW_UP_MIN_TIMEOUT,
            follow_up_seconds - (iteration - 1) * FOLLOW_UP_TIMEOUT_DECAY,
        )

        # TTS-tail drain is now handled inside listen_for_follow_up() via
        # the adaptive quiet-wait phase (v0.1.65) — proceed the moment
        # the room is actually quiet instead of guessing a fixed sleep.
        # Replaced the v0.1.64 follow_up_tts_settle_secs hack.
        elapsed = time.monotonic() - tts_end_ts
        history_secs = max(0.0, min(2.0, elapsed))
        logger.info(
            "Follow-up iteration begin",
            iteration=iteration,
            max_iterations=max_iterations,
            elapsed_since_tts=round(elapsed, 3),
            history_secs=round(history_secs, 3),
            timeout=round(iter_timeout, 1),
            consecutive_noise=consecutive_noise,
        )
        # Re-pause any player processes before listening. The conversation's
        # outer pause/restore wrapping (in start_voice_listener) only catches
        # players that existed at wake time — but a previous turn may have
        # *just spawned* a player (e.g. Pandora.play -> mpv) that needs to be
        # silenced for the follow-up capture too.
        #
        # Backgrounded (was synchronous): pkill+pactl cost ~400 ms here on
        # the Pi Zero even when nothing's playing, and it ran BEFORE the
        # listen window started — so 400 ms of every follow-up window was
        # spent on subprocess startup while user audio piled up in the bus.
        # If music IS playing, the pause lands within ~400 ms which is
        # negligible relative to the multi-second listen window.
        if _bg_executor is not None:
            _bg_executor.submit(pause_active_playback)
        else:
            pause_active_playback()
        # User-facing: signal the listening state on the LED so the user
        # knows the follow-up window is open. Without this the LEDs sit
        # on the idle pattern after the response, the user thinks the
        # system is done, and they don't speak — by the time they do
        # the window has expired. Cleared after listen returns regardless
        # of outcome.
        set_led_transient("listening")
        try:
            audio_file = listen_for_follow_up(
                bus, timeout_seconds=iter_timeout, history_secs=history_secs,
                follow_up_iteration=iteration,
                silence_threshold=silence_threshold,
                silence_duration=silence_duration,
            )
        finally:
            set_led_transient(None)
        if audio_file is None:
            logger.info("Follow-up window expired, returning to wake word mode",
                        iteration=iteration)
            break

        # Audio captured — switch to "thinking" so the user knows the
        # system has their input and is processing. Without this the
        # LED returns to idle between speech-detected and TTS-playing,
        # which reads as "it didn't hear me" — leading to re-speaking
        # over the eventual response.
        set_led_transient("thinking")
        try:
            # Prepend the (cached) wake-word audio for the speaker pass —
            # short follow-ups like "delete it" don't have enough material
            # for ECAPA to score reliably on their own.
            speaker_audio_path = try_build_speaker_audio(
                wake_audio_path, audio_file,
            )
            transcription_result = stt_provider.transcribe_with_speaker(
                audio_file, speaker_audio_path=speaker_audio_path,
            )
        except Exception as e:
            logger.warning("Follow-up transcription failed", error=str(e))
            set_led_transient(None)
            break

        if is_non_speech(transcription_result.text):
            logger.info(
                "Non-speech follow-up transcription, ending follow-up",
                text=transcription_result.text,
            )
            break

        text = transcription_result.text

        # Layer 3: Follow-up noise detection — catches short/repeated
        # transcriptions that slip past is_non_speech.  Two consecutive
        # noise-like results means the room is noisy, not that the user
        # is talking to us.
        if is_follow_up_noise(text, prev_text):
            consecutive_noise += 1
            logger.info(
                "Follow-up noise detected",
                text=text, prev_text=prev_text,
                consecutive_noise=consecutive_noise,
                max_consecutive=MAX_CONSECUTIVE_NOISE,
            )
            if consecutive_noise >= MAX_CONSECUTIVE_NOISE:
                logger.info("Too many consecutive noise transcriptions, ending follow-up")
                break
            prev_text = text
            # Don't process noise as a command — immediately re-listen.
            # Update tts_end_ts so next iteration doesn't replay stale audio.
            tts_end_ts = time.monotonic()
            continue

        # Layer 4: Self-echo detection — secondary defense behind the
        # TTS-drain settle at the top of the loop. If the captured
        # transcript ≥85% overlaps the assistant's most recent reply,
        # treat it as the mic re-hearing the TTS tail and exit follow-up
        # rather than feeding it back into CC (the phantom "Here is sad
        # music" loop). Hard exit, not consecutive_noise — a single
        # confirmed echo is enough; we'd rather end follow-up early than
        # risk another fake command.
        if looks_like_self_echo(text, last_assistant_text):
            logger.info(
                "Follow-up self-echo detected, ending follow-up",
                text=text,
                last_assistant_excerpt=last_assistant_text[:120],
            )
            break

        # Real speech — reset noise counter
        consecutive_noise = 0
        prev_text = text

        speaker_user_id = transcription_result.speaker_user_id
        logger.info("Follow-up speech received", text=text, conversation_id=conversation_id)

        # Start barge-in monitor for TTS playback (if OWW available)
        barge_in: BargeInMonitor | None = None
        if oww and barge_in_enabled():
            barge_in = BargeInMonitor(
                bus, oww, wake_word_model,
                threshold=barge_in_threshold(),
                energy_threshold=barge_in_energy_threshold(),
            )
            barge_in.start()

        should_break = False
        try:
            # Try pre-routing first (e.g., "stop", "pause")
            pre_result = command_service.try_pre_route(text, conversation_id or "")
            if pre_result is not None:
                command_service.speak_result(pre_result)
                last_assistant_text = extract_assistant_text(pre_result) or last_assistant_text
                # Pre-routed commands break the CC conversation context
                conversation_id = None
            elif conversation_id:
                # Continue existing conversation
                result = command_service.continue_conversation(
                    conversation_id, text, validation_handler,
                    turn_source="follow_up",
                    follow_up_iteration=iteration,
                )
                command_service.speak_result(result)
                last_assistant_text = extract_assistant_text(result) or last_assistant_text
                conversation_id = result.get("conversation_id", conversation_id)
                if result.get("clear_history"):
                    logger.info("Conversation complete (clear_history), ending follow-up")
                    should_break = True
                if result.get("not_for_me"):
                    logger.info("Follow-up result signalled not_for_me, ending follow-up")
                    should_break = True
                if result.get("end_of_exchange"):
                    logger.info("Follow-up result signalled end_of_exchange, ending follow-up")
                    should_break = True
            else:
                # No conversation context — start fresh. Still a follow-up
                # capture (no wake word fired), so CC keeps the strict
                # not_for_me posture for it.
                result = command_service.process_voice_command(
                    text, validation_handler, speaker_user_id=speaker_user_id,
                    turn_source="follow_up",
                    follow_up_iteration=iteration,
                )
                command_service.speak_result(result)
                last_assistant_text = extract_assistant_text(result) or last_assistant_text
                conversation_id = result.get("conversation_id") if result.get("success") else None
                if result.get("clear_history"):
                    logger.info("Conversation complete (clear_history), ending follow-up")
                    should_break = True
                if result.get("not_for_me"):
                    logger.info("Follow-up result signalled not_for_me, ending follow-up")
                    should_break = True
                if result.get("end_of_exchange"):
                    logger.info("Follow-up result signalled end_of_exchange, ending follow-up")
                    should_break = True

            # Capture TTS-end timestamp HERE (right after speak_result), not
            # after the finally block. barge_in.stop() routinely takes ~1.5s
            # to drain (oww.reset + thread.join), and that 1.5s is exactly
            # the window the user uses to start their next follow-up. If we
            # mark tts_end_ts after the finally, elapsed≈0 → history_secs≈0
            # → the user's speech in the gap is dropped on iter 2+.
            tts_end_ts = time.monotonic()

        except Exception as e:
            logger.warning("Follow-up processing failed, returning to wake word mode", error=str(e))
            break
        finally:
            if barge_in:
                barge_in.stop()

        if should_break:
            break
        if barge_in and barge_in.was_interrupted:
            logger.info("Barge-in during follow-up, returning to wake word mode")
            platform_audio.reset_cancel()
            # Same LED-reset as the main wake cycle — the follow-up's
            # transient ("speaking" during the response, "thinking" during
            # processing) must clear so the LED doesn't lie about state
            # after the user interrupts.
            set_led_transient(None)
            break

    # Wake cycle is done — notify CC so it can clear per-node speaker
    # stickiness (and any future lifecycle state). Best-effort: failures
    # are non-fatal, the next wake will just see a stale stickiness entry
    # bounded by the TTL.
    if conversation_id:
        try:
            command_service.client.end_conversation(conversation_id)
        except Exception as e:
            logger.warning("conversation/end raised after follow-up loop", error=str(e))
