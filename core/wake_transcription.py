"""Wake transcription pipeline — audio → STT → CC round-trip.

After the wake word fires, this module owns the round trip from
recorded audio to a final command result. Five public entry points:

  * :func:`try_capture_wake_audio` — snapshot the wake-word audio
    from the bus ring buffer. ``listen()`` runs with
    ``history_secs=0.0`` so Whisper's transcription stays clean; the
    snapshot is reused for the speaker pass instead.
  * :func:`try_build_speaker_audio` — concat the wake snapshot with
    the just-recorded command audio for the speaker pass. ECAPA needs
    the longer clip; Whisper still transcribes the command-only file.
  * :func:`speak_error` — shared error-path TTS that also flashes the
    LED red and falls back to a bundled error chime if TTS is itself
    unreachable.
  * :func:`make_validation_handler` — builds the closure CC calls when
    it needs the user to disambiguate a parameter (e.g. "which timer?").
  * :func:`send_for_transcription` — the orchestrator: STT round-trip,
    non-speech filtering, false-wake detection, CC round-trip, TTS-out.

Module-level state ``_last_speaker_user_id`` / ``_last_speaker_confidence``
is written by a successful ``send_for_transcription`` (when the speaker
pass identified someone) and read by the wake loop via
:func:`get_last_speaker` to seed warmup-thread args for the next turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict

from jarvis_log_client import JarvisLogger

from clients.responses.jarvis_command_center import ValidationRequest
from core.audio_bus import AudioBus
from core.false_wake import is_false_wake
from core.helpers import get_tts_provider
from core.platform_audio import platform_audio
from core.voice_filters import is_non_speech
from core.wake_response import set_led_transient
from scripts.speech_to_text import (
    RecordingResult,
    concat_wav_files,
    listen,
    snapshot_bus_to_wav,
    write_frames_to_wav,
)
from utils.config_service import Config
from utils.encryption_utils import get_cache_dir

if TYPE_CHECKING:
    from utils.command_execution_service import CommandExecutionService


logger = JarvisLogger(service="jarvis-node")


_cache_dir = get_cache_dir()
_ERRORS_DIR = Path(__file__).resolve().parent.parent / "sounds" / "errors"

# At wake-fire time we snapshot the bus ring buffer to capture the
# wake-word audio that's about to be discarded (``listen()`` runs with
# ``history_secs=0`` so Whisper's transcription stays clean). That
# snapshot is then concatenated with the just-recorded command audio
# and sent as the ``speaker_audio`` field to whisper-api — ECAPA
# scores the longer combined clip while Whisper still transcribes the
# command-only file. The same wake snapshot is reused for every
# follow-up in the same conversation since follow-ups have no wake
# word of their own.
_WAKE_AUDIO_PATH = _cache_dir / "wake.wav"
_SPEAKER_AUDIO_PATH = _cache_dir / "speaker.wav"
_WAKE_SNAPSHOT_SECONDS = 2.0

# Public alias for the wake loop: it sizes its consumed-chunks clip
# deque (the primary wake-clip source since v0.2.4) off the same
# duration the fallback bus snapshot uses.
WAKE_CLIP_SECONDS = _WAKE_SNAPSHOT_SECONDS


# Last identified speaker so parallel warmup can load their memories
# and CC can use it for per-node stickiness on short follow-up
# utterances. Updated after every successful transcription with
# speaker identification. Read by voice_listener via
# :func:`get_last_speaker`.
_last_speaker_user_id: int | None = None
_last_speaker_confidence: float | None = None


def get_last_speaker() -> tuple[int | None, float | None]:
    """Return the last identified speaker (user_id, confidence).

    The wake loop reads this to seed the warmup thread's args for the
    next conversation — by the time CC runs its first inference, the
    speaker's memories and preferences are already loaded.
    """
    return _last_speaker_user_id, _last_speaker_confidence


# ---------------------------------------------------------------------------
# Audio bundle assembly
# ---------------------------------------------------------------------------


def try_capture_wake_audio_from_frames(
    frames, bus: AudioBus,
) -> str | None:
    """Write wake.wav from the exact chunks the wake loop scored.

    Primary wake-clip path (v0.2.4). The loop hands over the deque of
    chunks it pulled from its subscriber queue — including any the
    drain-to-newest pass skipped scoring — so the clip is BY
    CONSTRUCTION the audio openWakeWord fired on. The ring-buffer
    snapshot (:func:`try_capture_wake_audio`) survives as the fallback:
    it cuts a variable wall-time after the fire, and post-turn producer
    catch-up bursts can evict the wake phrase from the ring by then —
    prod clips came back as post-phrase ambient ("in journals.") on
    real 0.97+ wakes, and wake verification then suppressed two REAL
    commands off those garbage clips.

    Returns the wake WAV path on success, None if there were no frames
    or the write failed (callers fall back to the bus snapshot).
    """
    if not frames:
        return None
    try:
        write_frames_to_wav(str(_WAKE_AUDIO_PATH), list(frames), bus)
    except Exception as e:
        logger.warning(
            "Wake-clip write from consumed chunks failed "
            "(falling back to bus snapshot)",
            error=str(e),
        )
        return None
    return str(_WAKE_AUDIO_PATH)


def try_capture_wake_audio(bus: AudioBus) -> str | None:
    """Snapshot the wake-word audio from the bus ring buffer.

    Fallback path — see :func:`try_capture_wake_audio_from_frames` for
    why the consumed-chunks clip is preferred.

    Returns the wake WAV path on success, None if nothing was captured
    or the write failed (callers proceed without speaker_audio in that
    case — recognition still works, just on the command-only clip).
    """
    try:
        captured = snapshot_bus_to_wav(
            bus, _WAKE_SNAPSHOT_SECONDS, str(_WAKE_AUDIO_PATH),
        )
    except Exception as e:
        logger.warning("Wake-audio snapshot failed (continuing without)", error=str(e))
        return None
    return str(_WAKE_AUDIO_PATH) if captured else None


def try_build_speaker_audio(
    wake_audio_path: str | None, command_audio_path: str,
) -> str | None:
    """Concat wake-word audio with the command audio for the speaker pass.

    Returns the concat WAV path, or None if there's no wake audio to
    prepend or the concat failed (STT falls back to single-file pass).
    """
    if not wake_audio_path:
        return None
    try:
        concat_wav_files(
            wake_audio_path, command_audio_path, str(_SPEAKER_AUDIO_PATH),
        )
    except Exception as e:
        logger.warning(
            "Speaker-audio concat failed (using command-only)",
            error=str(e),
        )
        return None
    return str(_SPEAKER_AUDIO_PATH)


# ---------------------------------------------------------------------------
# Error path
# ---------------------------------------------------------------------------


def speak_error(message: str) -> None:
    """Speak an error message, falling back to a bundled sound if TTS fails."""
    set_led_transient("error")
    try:
        tts = get_tts_provider()
        tts.speak(False, message)
    except Exception:
        chime = _ERRORS_DIR / "error_generic.wav"
        if chime.exists():
            platform_audio.play_audio_file(str(chime))
    finally:
        set_led_transient(None)


# ---------------------------------------------------------------------------
# Validation handler (CC <validation/> callback)
# ---------------------------------------------------------------------------


def make_validation_handler(
    bus: AudioBus, stt_provider,
) -> Callable[[ValidationRequest], str]:
    """Create a validation handler that prompts via TTS and re-listens."""
    def validation_handler(validation: ValidationRequest) -> str:
        tts_provider_instance = get_tts_provider()

        question = validation.question
        if validation.options:
            options_text = ", ".join(validation.options)
            question = f"{question} Your options are: {options_text}"

        logger.info("Asking validation question", question=question)
        tts_provider_instance.speak(False, question)

        logger.debug("Listening for validation response")
        validation_recording = listen(bus, history_secs=0.0)

        validation_transcription = stt_provider.transcribe(validation_recording.audio_file)

        if validation_transcription:
            logger.info("User validation response", response=validation_transcription)
            return validation_transcription
        else:
            logger.warning("Failed to transcribe validation response")
            return "I didn't catch that, sorry."

    return validation_handler


# ---------------------------------------------------------------------------
# The orchestrator
# ---------------------------------------------------------------------------


def send_for_transcription(
    recording: RecordingResult,
    command_service: "CommandExecutionService",
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
    warmup_thread=None,
    conversation_id: str | None = None,
    warmup_result: dict | None = None,
    skip_ack: bool = False,
    pre_wake_speech_seconds: float | None = None,
    wake_audio_path: str | None = None,
    wake_confidence: float | None = None,
    self_playback: bool | None = None,
    self_playback_kind: str | None = None,
) -> Dict[str, Any] | None:
    global _last_speaker_user_id, _last_speaker_confidence

    logger.info("Sending audio to transcription server")
    set_led_transient("thinking")

    # Build a longer speaker-pass clip (wake-word + command) when we
    # have the wake audio captured. Whisper still transcribes the
    # command-only file at recording.audio_file. See try_build_speaker_audio.
    speaker_audio_path = try_build_speaker_audio(
        wake_audio_path, recording.audio_file,
    )

    # STT with specific error handling
    try:
        result = stt_provider.transcribe_with_speaker(
            recording.audio_file, speaker_audio_path=speaker_audio_path,
            conversation_id=conversation_id,
        )
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.error("STT connection failed", error=str(e))
        speak_error("I'm having trouble connecting right now.")
        set_led_transient(None)
        return None
    except Exception as e:
        logger.error("STT failed", error=str(e))
        speak_error("I couldn't understand that, sorry.")
        set_led_transient(None)
        return None

    if is_non_speech(result.text):
        logger.info("Non-speech transcription, skipping", text=result.text)
        set_led_transient(None)
        return None

    if result.text and is_false_wake(result.text, recording, segments=result.segments):
        logger.info("False wake detected, aborting silently", text=result.text[:80],
                     duration=recording.duration, hit_max=recording.hit_max_duration,
                     num_segments=len(result.segments))
        set_led_transient(None)
        return None

    # ``result.text`` is guaranteed non-empty here: ``is_non_speech("")``
    # above returned True and short-circuited, so empty/whitespace text
    # has already returned.
    transcription = result.text
    speaker_user_id = result.speaker_user_id
    # Capture the acoustic-affect read now — ``result`` is reassigned below to the
    # command-processing return value.
    affect = result.affect
    if speaker_user_id:
        _last_speaker_user_id = speaker_user_id
        _last_speaker_confidence = result.speaker_confidence
        logger.info("Transcription received", text=transcription,
                    speaker_user_id=speaker_user_id, speaker_confidence=result.speaker_confidence)
    else:
        logger.info("Transcription received", text=transcription)

    # Command processing with specific error handling.
    # When wake_ack_audio_enabled=False the user has opted out of audio
    # acks entirely — no wake-time ack, no post-listen processing-ack,
    # no LLM-side "let me look into that" ack. The LED still flashes.
    audio_acks_disabled = not Config.get_bool("wake_ack_audio_enabled", True)
    try:
        result = command_service.process_voice_command(
            transcription, validation_handler,
            speaker_user_id=speaker_user_id,
            conversation_id=conversation_id,
            warmup_thread=warmup_thread,
            warmup_result=warmup_result,
            skip_ack=skip_ack or audio_acks_disabled,
            pre_wake_speech_seconds=pre_wake_speech_seconds,
            affect=affect,
            turn_source="wake",
            wake_confidence=wake_confidence,
            self_playback=self_playback,
            self_playback_kind=self_playback_kind,
        )
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.error("Command center unreachable", error=str(e))
        speak_error("I can't reach my server right now.")
        return None
    except Exception as e:
        logger.error("Command processing failed", error=str(e))
        speak_error("Something went wrong, sorry about that.")
        return None

    command_service.speak_result(result)
    return result
