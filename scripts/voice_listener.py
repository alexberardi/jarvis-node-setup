import os
import queue
import random
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator

import numpy as np
import openwakeword
from openwakeword.model import Model as OWWModel
import pyaudio
from scipy.signal import resample_poly
from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from core.audio_bus import AudioBus
from core.barge_in import BargeInMonitor
from core.helpers import get_tts_provider, get_stt_provider, get_wake_response_provider
from core.platform_audio import platform_audio
from scripts.speech_to_text import RecordingResult, listen, listen_for_follow_up
from services.alert_queue_service import get_alert_queue_service
from utils.config_service import Config
from utils.command_execution_service import CommandExecutionService
from utils.encryption_utils import get_cache_dir
from utils.service_discovery import get_command_center_url
from clients.responses.jarvis_command_center import ValidationRequest

logger = JarvisLogger(service="jarvis-node")

CHIME_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sounds", "chime.wav")
_cache_dir = get_cache_dir()
WAKE_FILE = _cache_dir / "next_wake_response.txt"
WAKE_AUDIO_FILE = _cache_dir / "next_wake_response.wav"
PROCESSING_ACK_FILE = _cache_dir / "next_processing_ack.wav"

# Short, snappy acks played immediately after recording ends to fill the
# dead air while STT + LLM process.  No LLM needed — just variety.
_PROCESSING_ACK_POOL: list[str] = [
    "One moment.",
    "Got it.",
    "Working on it.",
    "Let me check.",
    "On it.",
    "Give me a second.",
]

WAKE_WORD_MODEL = Config.get_str("wake_word_model", "hey_jarvis") or "hey_jarvis"
WAKE_WORD_THRESHOLD = Config.get_float("wake_word_threshold", 0.4)

# Barge-in: allow interrupting TTS with wake word
BARGE_IN_ENABLED = Config.get_str("barge_in_enabled", "true").lower() in ("true", "1", "yes")
BARGE_IN_THRESHOLD = Config.get_float("barge_in_threshold", 0.07)
BARGE_IN_ENERGY_THRESHOLD = Config.get_float("barge_in_energy_threshold", 500.0)

# openWakeWord needs 16 kHz audio in 1280-sample (80 ms) chunks
OWW_RATE = 16000
OWW_CHUNK = 1280

# Many USB mics only support 44100/48000 Hz — capture at 48 kHz and downsample
MIC_RATE = 48000
MIC_CHUNK = OWW_CHUNK * (MIC_RATE // OWW_RATE)  # 3840 samples at 48 kHz = 80 ms

_WAKE_CHIMES_DIR = Path(__file__).resolve().parent.parent / "sounds" / "wake"

# Track the last identified speaker so parallel warmup can load their memories.
# Updated after every successful transcription with speaker identification.
_last_speaker_user_id: int | None = None

# Shared AudioBus, set when start_voice_listener() initializes its bus.
# Other subsystems (e.g. enrollment-via-MQTT) need a way to consume mic
# audio without opening a competing PyAudio stream — they call
# ``get_audio_bus()`` and subscribe.
_audio_bus: AudioBus | None = None


def get_audio_bus() -> AudioBus | None:
    """Return the running AudioBus, or None if voice_listener hasn't started."""
    return _audio_bus


# When set, the main wake loop short-circuits its score check and yields
# the CPU. Used by transient flows that want to consume the mic via the
# bus without competing with wake detection — voice-profile enrollment
# in particular, where reading a sample prompt aloud near the mic would
# otherwise re-fire the wake detector and clash with the recording.
_wake_paused = threading.Event()

# Dedupe back-to-back wake fires for a single utterance.
# openWakeWord can score >threshold on consecutive 80ms chunks for one
# "Hey Jarvis", and the wake loop break can re-trigger before the
# conversation flow takes the lock. This guard ignores any wake whose
# previous trigger was less than _WAKE_DEBOUNCE_SEC ago.
_WAKE_DEBOUNCE_SEC = 8.0
_last_wake_ts: float = 0.0
_last_wake_lock = threading.Lock()


def pause_wake() -> None:
    """Disable wake detection until ``resume_wake()`` is called."""
    _wake_paused.set()
    logger.debug("Wake detection paused")


def resume_wake() -> None:
    """Re-enable wake detection."""
    _wake_paused.clear()
    logger.debug("Wake detection resumed")


@contextmanager
def wake_paused() -> Iterator[None]:
    """``with`` block that disables wake detection for its duration.

    Usage::

        from scripts.voice_listener import wake_paused
        with wake_paused():
            # capture mic via the bus without wake firing on what we hear
            ...
    """
    pause_wake()
    try:
        yield
    finally:
        resume_wake()


def _run_warmup(
    command_service: CommandExecutionService,
    conversation_id: str,
    speaker_user_id: int | None,
    result: dict,
) -> None:
    """Run conversation warmup in a background thread (during recording).

    Populates ``result["success"]`` so the caller can check whether the
    warmup succeeded after joining the thread.
    """
    try:
        success = command_service.register_tools_for_conversation(
            conversation_id, speaker_user_id=speaker_user_id,
        )
        result["success"] = success
    except Exception as e:
        logger.warning("Background warmup failed", error=str(e))
        result["success"] = False


def _bundled_wake_chimes() -> list[Path]:
    """List the pre-generated wake chime WAVs bundled with the node."""
    if not _WAKE_CHIMES_DIR.exists():
        return []
    return sorted(_WAKE_CHIMES_DIR.glob("*.wav"))


def handle_keyword_detected():
    t_enter = time.perf_counter()
    logger.info("Wake word detected, listening for command")
    print("Wake word detected! Listening...")

    # Priority order:
    # 1. WAKE_AUDIO_FILE (LLM-generated variety, cached on prior wake)
    # 2. Random pick from bundled sounds/wake/*.wav (always-present fallback)
    # 3. TTS speak (last-resort, requires network)
    played = False
    source = "none"
    file_size = 0

    if WAKE_AUDIO_FILE.exists():
        source = "cached_llm"
        try:
            file_size = WAKE_AUDIO_FILE.stat().st_size
        except OSError:
            pass
        t_pre = time.perf_counter()
        # Pause wake detection while we play the wake response — otherwise
        # the wake-word model can hear our own response audio and retrigger,
        # causing 2x playback (~2.7s of dead air for one "Hey Jarvis").
        try:
            with wake_paused():
                played = platform_audio.play_audio_file(str(WAKE_AUDIO_FILE))
        except Exception as e:
            logger.warning("Failed to play cached wake audio", error=str(e))
        finally:
            t_post = time.perf_counter()
            WAKE_AUDIO_FILE.unlink(missing_ok=True)
            WAKE_FILE.unlink(missing_ok=True)
            logger.info(
                f"wake audio timing | source={source} size={file_size}B "
                f"pre={int((t_pre - t_enter) * 1000)}ms "
                f"play={int((t_post - t_pre) * 1000)}ms "
                f"total={int((t_post - t_enter) * 1000)}ms"
            )

    if not played:
        bundled = _bundled_wake_chimes()
        if bundled:
            chime = random.choice(bundled)
            source = "bundled"
            try:
                file_size = chime.stat().st_size
            except OSError:
                pass
            t_pre = time.perf_counter()
            try:
                with wake_paused():
                    played = platform_audio.play_audio_file(str(chime))
                if played:
                    logger.debug("Played bundled wake chime", chime=chime.name)
            except Exception as e:
                logger.warning("Failed to play bundled wake chime", chime=chime.name, error=str(e))
            t_post = time.perf_counter()
            logger.info(
                f"wake audio timing | source={source} size={file_size}B "
                f"pre={int((t_pre - t_enter) * 1000)}ms "
                f"play={int((t_post - t_pre) * 1000)}ms "
                f"total={int((t_post - t_enter) * 1000)}ms"
            )

    if not played:
        tts_provider = get_tts_provider()
        if WAKE_FILE.exists():
            wake_text = WAKE_FILE.read_text().strip()
            WAKE_FILE.unlink(missing_ok=True)
        else:
            wake_text = "Yes?"
        tts_provider.speak(False, wake_text)

    # Fetch the next wake response in the background if provider is configured
    threading.Thread(target=fetch_next_wake_response, daemon=True).start()


def _trim_wav_silence(wav_bytes: bytes, threshold: int = 200) -> bytes:
    """Strip leading/trailing silence from a WAV byte string.

    TTS providers commonly bookend output with 200-400ms of silence which
    bloats cached wake responses (where every ms costs perceived latency).
    Threshold is the abs sample value below which a frame counts as silent;
    default 200 ≈ -42 dB at 16-bit, conservative enough to not clip speech.
    """
    import io
    import wave

    import numpy as np

    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(wav.getnframes())

    if params.sampwidth != 2:  # 16-bit only — bail on float / 24-bit
        return wav_bytes

    samples = np.frombuffer(frames, dtype=np.int16)
    if params.nchannels > 1:
        samples = samples.reshape(-1, params.nchannels)
        active = np.any(np.abs(samples) > threshold, axis=1)
    else:
        active = np.abs(samples) > threshold

    if not active.any():
        return wav_bytes  # nothing above threshold, leave it alone

    first = int(active.argmax())
    last = len(active) - 1 - int(active[::-1].argmax())

    # Keep ~5ms pad on each side so we don't clip plosives. Aggressive
    # because every ms of leading silence is perceived latency on wake.
    pad = int(params.framerate * 0.005)
    first = max(0, first - pad)
    last = min(len(active) - 1, last + pad)

    trimmed = samples[first : last + 1]

    out = io.BytesIO()
    with wave.open(out, "wb") as wav_out:
        wav_out.setparams(params)
        wav_out.writeframes(trimmed.tobytes())
    return out.getvalue()


def fetch_next_wake_response():
    """Fetch the next wake response text and pre-generate audio cache."""
    try:
        provider = get_wake_response_provider()
        if not provider:
            logger.debug("No wake response provider configured")
            return

        response_text = provider.fetch_next_wake_response()
        if not response_text:
            return

        WAKE_FILE.write_text(response_text)
        logger.debug("Stored next wake response", response=response_text)

        # Pre-generate audio so next wake word plays instantly
        command_center_url = get_command_center_url()
        if not command_center_url:
            return

        audio_bytes: bytes | None = RestClient.post_binary(
            f"{command_center_url}/api/v0/media/tts/speak",
            data={"text": response_text},
            timeout=30,
        )
        if audio_bytes:
            original_size = len(audio_bytes)
            try:
                audio_bytes = _trim_wav_silence(audio_bytes)
            except Exception as e:
                logger.debug("Silence trim failed, using original", error=str(e))
            WAKE_AUDIO_FILE.write_bytes(audio_bytes)
            logger.debug(
                "Cached wake response audio",
                size_bytes=len(audio_bytes),
                trimmed_from=original_size,
            )

    except Exception as e:
        logger.error("Failed to fetch next greeting", error=str(e))


def _play_processing_ack() -> bool:
    """Play the pre-cached processing ack in a background thread.

    Non-blocking so STT + LLM can start immediately — the ack is meant
    to MASK their latency, not precede it. Returning True tells the
    caller an ack will play, so it can suppress the delayed ack timer
    to avoid double-acking.
    """
    if not PROCESSING_ACK_FILE.exists():
        return False

    def _play_and_cleanup() -> None:
        try:
            platform_audio.play_audio_file(str(PROCESSING_ACK_FILE))
        except Exception as e:
            logger.warning("Failed to play processing ack", error=str(e))
        finally:
            PROCESSING_ACK_FILE.unlink(missing_ok=True)

    threading.Thread(target=_play_and_cleanup, daemon=True).start()
    return True


def _fetch_next_processing_ack() -> None:
    """Pre-generate a processing ack WAV for the next interaction.

    Mirrors :func:`fetch_next_wake_response` — picks a random short ack,
    synthesises audio via TTS, and caches it to disk so the next wake
    cycle can play it instantly after recording ends.
    """
    try:
        command_center_url = get_command_center_url()
        if not command_center_url:
            return

        text = random.choice(_PROCESSING_ACK_POOL)
        audio_bytes: bytes | None = RestClient.post_binary(
            f"{command_center_url}/api/v0/media/tts/speak",
            data={"text": text},
            timeout=15,
        )
        if audio_bytes:
            PROCESSING_ACK_FILE.write_bytes(audio_bytes)
            logger.debug("Cached processing ack audio", text=text, size_bytes=len(audio_bytes))
    except Exception as e:
        logger.debug("Failed to pre-generate processing ack (non-fatal)", error=str(e))


def _make_validation_handler(bus: AudioBus, stt_provider) -> Callable[[ValidationRequest], str]:
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
        validation_recording = listen(bus, history_secs=0.0, skip_secs=0.3)

        validation_transcription = stt_provider.transcribe(validation_recording.audio_file)

        if validation_transcription:
            logger.info("User validation response", response=validation_transcription)
            return validation_transcription
        else:
            logger.warning("Failed to transcribe validation response")
            return "I didn't catch that, sorry."

    return validation_handler


def _is_non_speech(text: str | None) -> bool:
    """True if Whisper output is a non-speech annotation like [BLANK_AUDIO]
    or (wind blowing) rather than an actual utterance. Whisper emits these
    for silence/noise, and treating them as commands keeps the follow-up
    loop alive forever when the node is near a fan or other constant noise.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    return (
        (stripped.startswith("[") and stripped.endswith("]"))
        or (stripped.startswith("(") and stripped.endswith(")"))
    )


_ABORT_PHRASES: set[str] = {
    "never mind", "nevermind", "cancel", "forget it",
    "that wasn't for you", "not you", "sorry jarvis",
    "ignore that", "ignore me",
}


def _is_false_wake(transcription: str, recording: RecordingResult) -> bool:
    """Detect false wake word triggers from ambient conversation.

    Uses a combination of signals:
    1. Abort phrases — user heard the chime and wants to cancel
    2. Max recording duration + long/mid-sentence transcription — ambient speech
    """
    raw = transcription.strip()
    text = raw.lower()

    # Signal 1: abort phrases
    for phrase in _ABORT_PHRASES:
        if text == phrase or text.startswith(phrase):
            return True

    # Signal 2: recording hit max duration (speaker never paused)
    if recording.hit_max_duration:
        words = text.split()
        # Long transcription — ambient conversation, not a command
        if len(words) > 20:
            return True
        # Starts mid-sentence (lowercase in original, not "i" or "ok")
        if raw and raw[0].islower() and not text.startswith(("i ", "i'", "ok")):
            return True

    return False


_ERRORS_DIR = Path(__file__).resolve().parent.parent / "sounds" / "errors"


def _speak_error(message: str) -> None:
    """Speak an error message, falling back to a bundled sound if TTS fails."""
    try:
        tts = get_tts_provider()
        tts.speak(False, message)
    except Exception:
        chime = _ERRORS_DIR / "error_generic.wav"
        if chime.exists():
            platform_audio.play_audio_file(str(chime))


def send_for_transcription(
    recording: RecordingResult,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
    warmup_thread: threading.Thread | None = None,
    conversation_id: str | None = None,
    warmup_result: dict | None = None,
    skip_ack: bool = False,
) -> Dict[str, Any] | None:
    global _last_speaker_user_id

    logger.info("Sending audio to transcription server")

    # STT with specific error handling
    try:
        result = stt_provider.transcribe_with_speaker(recording.audio_file)
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.error("STT connection failed", error=str(e))
        _speak_error("I'm having trouble connecting right now.")
        return None
    except Exception as e:
        logger.error("STT failed", error=str(e))
        _speak_error("I couldn't understand that, sorry.")
        return None

    if _is_non_speech(result.text):
        logger.info("Non-speech transcription, skipping", text=result.text)
        return None

    if result.text and _is_false_wake(result.text, recording):
        logger.info("False wake detected, aborting silently", text=result.text[:80],
                     duration=recording.duration, hit_max=recording.hit_max_duration)
        return None

    if result.text:
        transcription = result.text
        speaker_user_id = result.speaker_user_id
        if speaker_user_id:
            _last_speaker_user_id = speaker_user_id
            logger.info("Transcription received", text=transcription,
                        speaker_user_id=speaker_user_id, speaker_confidence=result.speaker_confidence)
        else:
            logger.info("Transcription received", text=transcription)

        # Command processing with specific error handling
        try:
            result = command_service.process_voice_command(
                transcription, validation_handler,
                speaker_user_id=speaker_user_id,
                conversation_id=conversation_id,
                warmup_thread=warmup_thread,
                warmup_result=warmup_result,
                skip_ack=skip_ack,
            )
        except (ConnectionError, OSError, TimeoutError) as e:
            logger.error("Command center unreachable", error=str(e))
            _speak_error("I can't reach my server right now.")
            return None
        except Exception as e:
            logger.error("Command processing failed", error=str(e))
            _speak_error("Something went wrong, sorry about that.")
            return None

        command_service.speak_result(result)
        return result
    else:
        _speak_error("I couldn't understand that, sorry.")
        return None


def _follow_up_loop(
    bus: AudioBus,
    initial_result: dict | None,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
    oww=None,
    tts_end_ts: float | None = None,
) -> None:
    """Listen for follow-up speech after TTS completes.

    If the user speaks within the follow-up window, process it as a
    continuation of the conversation. Each successful follow-up restarts
    the timer. Silence or error breaks out to wake word mode.

    If ``oww`` is provided, barge-in monitoring is active during TTS
    playback — the wake word interrupts the response and returns to the
    main wake detection loop.
    """
    # Default bumped 5→10s on 2026-04-25: the window opens as soon as the
    # caller returns from playing the response, but in practice the user
    # often needs 2-3s to react. 5s often expired before the user could
    # start speaking, making follow-ups feel "hit or miss". 10s gives
    # comfortable headroom; users can override via config.
    follow_up_seconds: float = Config.get_float("follow_up_listen_seconds", 10.0)
    if follow_up_seconds <= 0:
        return

    conversation_id: str | None = None
    if initial_result and initial_result.get("success"):
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

    while True:
        iteration += 1
        elapsed = time.monotonic() - tts_end_ts
        history_secs = max(0.0, min(2.0, elapsed))
        logger.info(
            "Follow-up iteration begin",
            iteration=iteration,
            elapsed_since_tts=round(elapsed, 3),
            history_secs=round(history_secs, 3),
            timeout=follow_up_seconds,
        )
        audio_file = listen_for_follow_up(
            bus, timeout_seconds=follow_up_seconds, history_secs=history_secs,
        )
        if audio_file is None:
            logger.info("Follow-up window expired, returning to wake word mode",
                        iteration=iteration)
            break

        try:
            transcription_result = stt_provider.transcribe_with_speaker(audio_file)
        except Exception as e:
            logger.warning("Follow-up transcription failed", error=str(e))
            break

        if _is_non_speech(transcription_result.text):
            logger.info(
                "Non-speech follow-up transcription, ending follow-up",
                text=transcription_result.text,
            )
            break

        text = transcription_result.text
        speaker_user_id = transcription_result.speaker_user_id
        logger.info("Follow-up speech received", text=text, conversation_id=conversation_id)

        # Start barge-in monitor for TTS playback (if OWW available)
        barge_in: BargeInMonitor | None = None
        if oww and BARGE_IN_ENABLED:
            barge_in = BargeInMonitor(
                bus, oww, WAKE_WORD_MODEL,
                threshold=BARGE_IN_THRESHOLD,
                energy_threshold=BARGE_IN_ENERGY_THRESHOLD,
            )
            barge_in.start()

        try:
            # Try pre-routing first (e.g., "stop", "pause")
            pre_result = command_service.try_pre_route(text, conversation_id or "")
            if pre_result is not None:
                command_service.speak_result(pre_result)
                # Pre-routed commands break the CC conversation context
                conversation_id = None
            elif conversation_id:
                # Continue existing conversation
                result = command_service.continue_conversation(
                    conversation_id, text, validation_handler
                )
                command_service.speak_result(result)
                conversation_id = result.get("conversation_id", conversation_id)
            else:
                # No conversation context — start fresh
                result = command_service.process_voice_command(
                    text, validation_handler, speaker_user_id=speaker_user_id
                )
                command_service.speak_result(result)
                conversation_id = result.get("conversation_id") if result.get("success") else None

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

        if barge_in and barge_in.was_interrupted:
            logger.info("Barge-in during follow-up, returning to wake word mode")
            platform_audio.reset_cancel()
            break


ALERT_ANNOUNCE_PRIORITY = 3  # Only announce priority >= this (reminders, urgent)
INLINE_LISTEN_TIMEOUT = 8.0  # Seconds to wait for snooze/dismiss after announcement


def _drain_alert_announcements(
    bus: AudioBus,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
) -> bool:
    """Check for high-priority alerts and announce them via TTS.

    Returns True if any announcements were made (caller should reopen stream).
    """
    try:
        queue = get_alert_queue_service()
        pending = queue.get_pending()
    except Exception:
        return False

    # Filter to only high-priority alerts (reminders, urgent emails)
    announcements = [a for a in pending if a.priority >= ALERT_ANNOUNCE_PRIORITY]
    if not announcements:
        return False

    tts_provider = get_tts_provider()

    for alert in announcements:
        logger.info("Announcing alert", title=alert.title, priority=alert.priority)

        # Speak the alert
        try:
            tts_provider.speak(True, alert.summary)
        except Exception as e:
            logger.warning("Alert TTS failed", error=str(e))
            continue

        # Inline listen for response (snooze/dismiss/silence)
        try:
            audio_file = listen_for_follow_up(bus, timeout_seconds=INLINE_LISTEN_TIMEOUT)
            if audio_file is None:
                logger.debug("No response to alert announcement (silence)")
                continue

            transcription_result = stt_provider.transcribe_with_speaker(audio_file)
            if not transcription_result.text:
                continue

            text = transcription_result.text
            speaker_user_id = transcription_result.speaker_user_id
            logger.info("Alert response received", text=text)

            # Process the response (e.g., "snooze", "snooze for 20 minutes", "got it")
            result = command_service.process_voice_command(
                text, validation_handler, speaker_user_id=speaker_user_id,
            )
            command_service.speak_result(result)

        except Exception as e:
            logger.warning("Inline listen after alert failed", error=str(e))

    # Flush the announced alerts from the queue
    try:
        queue.flush()
    except Exception:
        pass

    return True


def _start_keyboard_listener(bus: AudioBus | None = None) -> None:
    """Fallback listener: press Enter to trigger a command (no wake word).

    If ``bus`` is None, a standalone bus is created and started for the
    duration of the session — so this works both as a fallback after
    openwakeword init failure and as an opt-in dev-mode input.
    """
    logger.info("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")
    print("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")

    owns_bus = bus is None
    if bus is None:
        bus = AudioBus(rate=MIC_RATE, chunk_samples=MIC_CHUNK, history_secs=2.0)
        bus.start()

    command_service = CommandExecutionService()
    stt_provider = get_stt_provider()
    validation_handler = _make_validation_handler(bus, stt_provider)

    try:
        while True:
            input()  # block until Enter
            try:
                handle_keyword_detected()
            except Exception as e:
                logger.warning("Wake response TTS failed, continuing", error=str(e))

            # Parallel warmup during recording
            conversation_id = str(uuid.uuid4())
            warmup_result: dict = {"success": False}
            warmup_thread = threading.Thread(
                target=_run_warmup,
                args=(command_service, conversation_id, _last_speaker_user_id, warmup_result),
                daemon=True,
            )
            warmup_thread.start()

            recording = listen(bus, history_secs=0.0, skip_secs=0.3)

            ack_played = _play_processing_ack()

            start = time.perf_counter()
            result = send_for_transcription(
                recording, command_service, stt_provider, validation_handler,
                warmup_thread=warmup_thread,
                conversation_id=conversation_id,
                warmup_result=warmup_result,
                skip_ack=ack_played,
            )
            tts_end_ts = time.monotonic()
            end = time.perf_counter()

            logger.info("Transcription complete", duration_seconds=round(end - start, 2))

            _follow_up_loop(bus, result, command_service, stt_provider, validation_handler, tts_end_ts=tts_end_ts)

            # Pre-generate the next processing ack in the background
            threading.Thread(target=_fetch_next_processing_ack, daemon=True).start()

            logger.info("Press Enter to speak another command")
    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        if owns_bus:
            bus.stop()


def start_voice_listener(ma_service):
    """Main voice loop: wake → respond → listen → process → follow-up.

    One long-running AudioBus owns the mic for the lifetime of the node.
    Every audio consumer (wake detector, barge-in, command listen,
    follow-up) subscribes to the bus instead of opening its own PyAudio
    stream. This eliminates the concurrent-dsnoop-open race that caused
    BLANK_AUDIO captures in the pre-AudioBus implementation.

    Flow per iteration:
      1. Subscribe ``wake`` on the bus (48 kHz chunks).
      2. Downsample each chunk to 16 kHz and score with openWakeWord.
      3. On wake, unsubscribe ``wake`` so the wake detector doesn't
         fight the command listener for the queue.
      4. Play wake response (blocking TTS).
      5. Record the command via ``listen(bus, history_secs=0.0, skip_secs=0.3)`` —
         the 0.3s skip dodges TTS-tail bleed / AEC recovery without
         replaying the tail of the wake response into the recording
         (which would otherwise cause the node to transcribe its own
         TTS and respond to itself — "talking to itself" bug).
      6. Start barge-in monitor (also a bus subscriber) during
         STT → CC → TTS response playback.
      7. On barge-in OR normal completion, run the follow-up loop.
      8. Back to step 1 with a fresh ``wake`` subscription.
    """
    try:
        openwakeword.utils.download_models(model_names=[WAKE_WORD_MODEL])
        oww = OWWModel(wakeword_models=[WAKE_WORD_MODEL], inference_framework="onnx")
    except Exception as e:
        logger.warning("openWakeWord init failed, falling back to keyboard trigger", error=str(e))
        if sys.stdin and sys.stdin.isatty():
            _start_keyboard_listener()
        else:
            logger.error("No TTY available for keyboard fallback, exiting")
        return

    # Retry bus start — USB mic may not be ready immediately after boot.
    _audio_retry_delays: list[int] = [2, 2, 5, 5, 10, 10, 15, 15, 30, 30, 30, 30]
    bus: AudioBus | None = None
    for attempt, delay in enumerate(_audio_retry_delays):
        try:
            bus = AudioBus(rate=MIC_RATE, chunk_samples=MIC_CHUNK, history_secs=2.0)
            bus.start()
            break
        except OSError as e:
            logger.warning("Audio device unavailable",
                           error=str(e), attempt=attempt + 1,
                           max_attempts=len(_audio_retry_delays),
                           retry_in_seconds=delay)
            if bus is not None:
                try:
                    bus.stop()
                except Exception:
                    pass
                bus = None
            time.sleep(delay)

    if bus is None:
        logger.error("No audio device found after retries, giving up",
                     total_attempts=len(_audio_retry_delays))
        return

    # Expose the running bus to other subsystems that need to consume mic
    # audio (e.g. MQTT-triggered voice enrollment).
    global _audio_bus
    _audio_bus = bus

    command_service = CommandExecutionService()
    stt_provider = get_stt_provider()
    validation_handler = _make_validation_handler(bus, stt_provider)

    # Pre-warm the LLM's KV cache and processing ack on boot.
    threading.Thread(
        target=_run_warmup,
        args=(command_service, str(uuid.uuid4()), None, {}),
        daemon=True,
    ).start()
    threading.Thread(target=_fetch_next_processing_ack, daemon=True).start()

    logger.info("Waiting for wake word", model=WAKE_WORD_MODEL,
                threshold=WAKE_WORD_THRESHOLD)
    print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}' (threshold={WAKE_WORD_THRESHOLD})")

    resample_down = bus.rate // OWW_RATE  # 3 for 48 kHz → 16 kHz
    alert_check_interval = 60             # ~every 5s at 80 ms chunks
    alert_check_counter = 0

    try:
        while True:
            # Safety net: ensure no stale cancel state from a prior
            # barge-in prevents wake-response or other audio.
            platform_audio.reset_cancel()

            wake_q = bus.subscribe("wake")
            score = 0.0
            try:
                was_paused = False
                while True:
                    alert_check_counter += 1
                    if alert_check_counter >= alert_check_interval:
                        alert_check_counter = 0
                        try:
                            aq = get_alert_queue_service()
                            has_announcements = any(
                                a.priority >= ALERT_ANNOUNCE_PRIORITY
                                for a in aq.get_pending()
                            )
                        except Exception:
                            has_announcements = False
                        if has_announcements:
                            # Let the alert drain run — it uses the bus too.
                            break  # ← exits inner loop with score==0; see below

                    try:
                        raw_data = wake_q.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    # While paused, drop the chunk and don't score it. The
                    # queue still drains so we don't process stale audio
                    # the moment we resume.
                    if _wake_paused.is_set():
                        was_paused = True
                        continue

                    # First chunk after a pause: reset the openWakeWord LSTM
                    # state. Without this, residual context from before the
                    # pause (often the wake response audio echoing back)
                    # immediately re-triggers a wake event.
                    if was_paused:
                        oww.reset()
                        was_paused = False

                    samples = np.frombuffer(raw_data, dtype=np.int16)
                    if resample_down > 1:
                        resampled = resample_poly(samples, up=1, down=resample_down)
                        samples = np.clip(resampled, -32768, 32767).astype(np.int16)

                    predictions = oww.predict(samples)
                    score = predictions.get(WAKE_WORD_MODEL, 0)
                    if score > 0.05:
                        logger.debug(
                            "Wake word score",
                            score=round(float(score), 3),
                            threshold=WAKE_WORD_THRESHOLD,
                        )
                    if score > WAKE_WORD_THRESHOLD:
                        break
            finally:
                bus.unsubscribe("wake")

            # If we broke out without a wake (alert-drain case), handle
            # alerts and loop.
            if score <= WAKE_WORD_THRESHOLD:
                try:
                    _drain_alert_announcements(
                        bus, command_service, stt_provider, validation_handler,
                    )
                except Exception as e:
                    logger.warning("Alert drain failed", error=str(e))
                oww.reset()
                print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}'")
                continue

            oww.reset()

            try:
                handle_keyword_detected()
            except Exception as e:
                logger.warning("Wake response TTS failed, continuing", error=str(e))

            conversation_id = str(uuid.uuid4())
            warmup_result: dict = {"success": False}
            warmup_thread = threading.Thread(
                target=_run_warmup,
                args=(command_service, conversation_id, _last_speaker_user_id, warmup_result),
                daemon=True,
            )
            warmup_thread.start()

            barge_in: BargeInMonitor | None = None
            if BARGE_IN_ENABLED:
                barge_in = BargeInMonitor(
                    bus, oww, WAKE_WORD_MODEL,
                    threshold=BARGE_IN_THRESHOLD,
                    energy_threshold=BARGE_IN_ENERGY_THRESHOLD,
                )

            result = None
            try:
                # history_secs=0 + skip_secs=0.3: do NOT replay the
                # wake-response TTS tail (that bug made the node
                # transcribe and respond to itself).
                recording = listen(bus, history_secs=0.0, skip_secs=0.3)

                ack_played = _play_processing_ack()

                if barge_in:
                    barge_in.start()

                start = time.perf_counter()
                result = send_for_transcription(
                    recording, command_service, stt_provider, validation_handler,
                    warmup_thread=warmup_thread,
                    conversation_id=conversation_id,
                    warmup_result=warmup_result,
                    skip_ack=ack_played,
                )
                # Capture TTS-end time RIGHT after send_for_transcription
                # returns (which is right after speak_result completes).
                # The follow-up loop uses this to know how far back to look
                # in the bus history for speech the user uttered before the
                # listener subscribed. barge_in.stop() in the finally below
                # routinely costs ~1.5s — that 1.5s is exactly the gap users
                # speak into.
                tts_end_ts = time.monotonic()
                end = time.perf_counter()
                logger.info("Transcription complete", duration_seconds=round(end - start, 2))
            except Exception as e:
                logger.warning("Command processing failed, resuming listener", error=str(e))
                print(f"Command failed: {e}")
                tts_end_ts = time.monotonic()
            finally:
                if barge_in:
                    barge_in.stop()

            if barge_in and barge_in.was_interrupted:
                logger.info("Barge-in: TTS interrupted, returning to wake word")
                platform_audio.reset_cancel()
                # Don't try to capture a new command here — the user
                # interrupted to STOP the response.  They'll say the
                # wake word again when they're ready.
            else:
                try:
                    _follow_up_loop(bus, result, command_service, stt_provider, validation_handler, oww=oww, tts_end_ts=tts_end_ts)
                except Exception as e:
                    logger.warning("Follow-up loop error, resuming wake word", error=str(e))

            threading.Thread(target=_fetch_next_processing_ack, daemon=True).start()
            print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}'")

    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        bus.stop()
        pa.terminate()
        del oww
