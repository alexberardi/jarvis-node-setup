import os
import random
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict

import numpy as np
import openwakeword
from openwakeword.model import Model as OWWModel
import pyaudio
from scipy.signal import resample_poly
from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from core.helpers import get_tts_provider, get_stt_provider, get_wake_response_provider
from core.platform_audio import platform_audio
from scripts.speech_to_text import listen, listen_for_follow_up
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

WAKE_WORD_MODEL = Config.get_str("wake_word_model", "hey_jarvis") or "hey_jarvis"
WAKE_WORD_THRESHOLD = Config.get_float("wake_word_threshold", 0.4)

# openWakeWord needs 16 kHz audio in 1280-sample (80 ms) chunks
OWW_RATE = 16000
OWW_CHUNK = 1280

# Many USB mics only support 44100/48000 Hz — capture at 48 kHz and downsample
MIC_RATE = 48000
MIC_CHUNK = OWW_CHUNK * (MIC_RATE // OWW_RATE)  # 3840 samples at 48 kHz = 80 ms

_mic_index_str: str | None = Config.get_str("mic_device_index")
MIC_DEVICE_INDEX: int | None = int(_mic_index_str) if _mic_index_str is not None else None


_WAKE_CHIMES_DIR = Path(__file__).resolve().parent.parent / "sounds" / "wake"


def _bundled_wake_chimes() -> list[Path]:
    """List the pre-generated wake chime WAVs bundled with the node."""
    if not _WAKE_CHIMES_DIR.exists():
        return []
    return sorted(_WAKE_CHIMES_DIR.glob("*.wav"))


def handle_keyword_detected():
    logger.info("Wake word detected, listening for command")
    print("Wake word detected! Listening...")

    # Priority order:
    # 1. WAKE_AUDIO_FILE (LLM-generated variety, cached on prior wake)
    # 2. Random pick from bundled sounds/wake/*.wav (always-present fallback)
    # 3. TTS speak (last-resort, requires network)
    played = False

    if WAKE_AUDIO_FILE.exists():
        try:
            played = platform_audio.play_audio_file(str(WAKE_AUDIO_FILE))
        except Exception as e:
            logger.warning("Failed to play cached wake audio", error=str(e))
        finally:
            WAKE_AUDIO_FILE.unlink(missing_ok=True)
            WAKE_FILE.unlink(missing_ok=True)

    if not played:
        bundled = _bundled_wake_chimes()
        if bundled:
            chime = random.choice(bundled)
            try:
                played = platform_audio.play_audio_file(str(chime))
                if played:
                    logger.debug("Played bundled wake chime", chime=chime.name)
            except Exception as e:
                logger.warning("Failed to play bundled wake chime", chime=chime.name, error=str(e))

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
            WAKE_AUDIO_FILE.write_bytes(audio_bytes)
            logger.debug("Cached wake response audio", size_bytes=len(audio_bytes))

    except Exception as e:
        logger.error("Failed to fetch next greeting", error=str(e))


def _make_validation_handler(stt_provider) -> Callable[[ValidationRequest], str]:
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
        validation_audio = listen()

        validation_transcription = stt_provider.transcribe(validation_audio)

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


def send_for_transcription(
    filename: str,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
) -> Dict[str, Any] | None:
    logger.info("Sending audio to transcription server")
    result = stt_provider.transcribe_with_speaker(filename)

    if _is_non_speech(result.text):
        logger.info("Non-speech transcription, skipping", text=result.text)
        return None

    if result.text:
        transcription = result.text
        speaker_user_id = result.speaker_user_id
        if speaker_user_id:
            logger.info("Transcription received", text=transcription,
                        speaker_user_id=speaker_user_id, speaker_confidence=result.speaker_confidence)
        else:
            logger.info("Transcription received", text=transcription)

        result = command_service.process_voice_command(
            transcription, validation_handler, speaker_user_id=speaker_user_id
        )
        command_service.speak_result(result)

        return result
    else:
        tts_provider = get_tts_provider()
        tts_provider.speak(False, "An error occurred during transcription")
        return None


def _follow_up_loop(
    initial_result: dict | None,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
) -> None:
    """Listen for follow-up speech after TTS completes.

    If the user speaks within the follow-up window, process it as a
    continuation of the conversation. Each successful follow-up restarts
    the timer. Silence or error breaks out to wake word mode.
    """
    follow_up_seconds: float = Config.get_float("follow_up_listen_seconds", 5.0)
    if follow_up_seconds <= 0:
        return

    conversation_id: str | None = None
    if initial_result and initial_result.get("success"):
        conversation_id = initial_result.get("conversation_id")

    while True:
        audio_file = listen_for_follow_up(timeout_seconds=follow_up_seconds)
        if audio_file is None:
            logger.debug("Follow-up window expired, returning to wake word mode")
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

        except Exception as e:
            logger.warning("Follow-up processing failed, returning to wake word mode", error=str(e))
            break


ALERT_ANNOUNCE_PRIORITY = 3  # Only announce priority >= this (reminders, urgent)
INLINE_LISTEN_TIMEOUT = 8.0  # Seconds to wait for snooze/dismiss after announcement


def _drain_alert_announcements(
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
            audio_file = listen_for_follow_up(timeout_seconds=INLINE_LISTEN_TIMEOUT)
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


def _start_keyboard_listener() -> None:
    """Fallback listener: press Enter to trigger a command (no wake word)."""
    logger.info("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")
    print("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")

    command_service = CommandExecutionService()
    stt_provider = get_stt_provider()
    validation_handler = _make_validation_handler(stt_provider)

    try:
        while True:
            input()  # block until Enter
            try:
                handle_keyword_detected()
            except Exception as e:
                logger.warning("Wake response TTS failed, continuing", error=str(e))

            audio_file = listen()

            start = time.perf_counter()
            result = send_for_transcription(audio_file, command_service, stt_provider, validation_handler)
            end = time.perf_counter()

            logger.info("Transcription complete", duration_seconds=round(end - start, 2))

            _follow_up_loop(result, command_service, stt_provider, validation_handler)

            logger.info("Press Enter to speak another command")
    except KeyboardInterrupt:
        logger.info("Stopping voice listener")


def _create_oww_stream(pa_instance: pyaudio.PyAudio) -> tuple:
    """Open a mono input stream for openWakeWord.

    Tries native 16 kHz first.  If the device doesn't support it, falls back
    to 48 kHz (which will be downsampled before feeding to the model).

    Returns:
        (stream, needs_resample) — needs_resample is True when capturing at 48 kHz.
    """
    base_kwargs: dict = dict(
        channels=1,
        format=pyaudio.paInt16,
        input=True,
    )
    if MIC_DEVICE_INDEX is not None:
        base_kwargs["input_device_index"] = MIC_DEVICE_INDEX

    # Try native 16 kHz first
    try:
        stream = pa_instance.open(**base_kwargs, rate=OWW_RATE, frames_per_buffer=OWW_CHUNK)
        return stream, False
    except OSError:
        pass

    # Fall back to 48 kHz — we'll downsample in the read loop
    stream = pa_instance.open(**base_kwargs, rate=MIC_RATE, frames_per_buffer=MIC_CHUNK)
    logger.info("Mic does not support 16 kHz, capturing at 48 kHz with resample")
    return stream, True


def start_voice_listener(ma_service):
    # Download model if needed and initialise openWakeWord
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

    # Retry audio init — USB mic may not be ready immediately after boot.
    # Exponential backoff: ~3 min total (down from 5 min with flat 10s delays).
    _audio_retry_delays: list[int] = [2, 2, 5, 5, 10, 10, 15, 15, 30, 30, 30, 30]
    pa = None
    oww_stream = None
    needs_resample = False
    for attempt, delay in enumerate(_audio_retry_delays):
        try:
            pa = pyaudio.PyAudio()

            # Log available input devices on first attempt for debugging
            if attempt == 0:
                device_count: int = pa.get_device_count()
                input_devices: list[str] = []
                for i in range(device_count):
                    info = pa.get_device_info_by_index(i)
                    if info.get("maxInputChannels", 0) > 0:
                        input_devices.append(f"{i}: {info['name']}")
                if input_devices:
                    logger.info("Available input devices", devices=input_devices)
                else:
                    logger.warning("No input audio devices found")

            oww_stream, needs_resample = _create_oww_stream(pa)
            break
        except OSError as e:
            if pa is not None:
                pa.terminate()
                pa = None
            logger.warning("Audio device unavailable",
                           error=str(e), attempt=attempt + 1,
                           max_attempts=len(_audio_retry_delays),
                           retry_in_seconds=delay)
            time.sleep(delay)

    if oww_stream is None:
        logger.error("No audio device found after retries, giving up",
                     total_attempts=len(_audio_retry_delays))
        return

    chunk_size = MIC_CHUNK if needs_resample else OWW_CHUNK

    # Create shared services once for the lifetime of the listener
    command_service = CommandExecutionService()
    stt_provider = get_stt_provider()
    validation_handler = _make_validation_handler(stt_provider)

    logger.info("Waiting for wake word", model=WAKE_WORD_MODEL,
                threshold=WAKE_WORD_THRESHOLD, resample=needs_resample)
    print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}' (threshold={WAKE_WORD_THRESHOLD})")

    # Alert drain interval: check every ~5 seconds (each chunk is ~80ms)
    alert_check_interval = 60  # reads between alert checks
    alert_check_counter = 0

    try:
        while True:
            # Periodically check for high-priority alerts to announce
            alert_check_counter += 1
            if alert_check_counter >= alert_check_interval:
                alert_check_counter = 0

                # Quick check without closing stream — only close if there's work to do
                try:
                    queue = get_alert_queue_service()
                    has_announcements = any(
                        a.priority >= ALERT_ANNOUNCE_PRIORITY
                        for a in queue.get_pending()
                    )
                except Exception:
                    has_announcements = False

                if has_announcements:
                    try:
                        # Stop stream before announcing (TTS + mic contention)
                        oww_stream.stop_stream()
                        oww_stream.close()
                        pa.terminate()

                        _drain_alert_announcements(
                            command_service, stt_provider, validation_handler,
                        )

                        # Reopen stream
                        pa = pyaudio.PyAudio()
                        oww_stream, needs_resample = _create_oww_stream(pa)
                        chunk_size = MIC_CHUNK if needs_resample else OWW_CHUNK
                        oww.reset()
                        print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}'")
                    except Exception as e:
                        logger.warning("Alert drain failed, reopening stream", error=str(e))
                        try:
                            pa = pyaudio.PyAudio()
                            oww_stream, needs_resample = _create_oww_stream(pa)
                            chunk_size = MIC_CHUNK if needs_resample else OWW_CHUNK
                        except Exception as e2:
                            logger.error("Failed to reopen stream after alert drain", error=str(e2))
                            break

            raw_data = oww_stream.read(chunk_size, exception_on_overflow=False)
            samples = np.frombuffer(raw_data, dtype=np.int16)

            if needs_resample:
                # Downsample 48 kHz → 16 kHz (factor of 3)
                resampled = resample_poly(samples, up=1, down=3)
                samples = np.clip(resampled, -32768, 32767).astype(np.int16)

            predictions = oww.predict(samples)
            score = predictions.get(WAKE_WORD_MODEL, 0)
            if score > 0.05:
                logger.debug("Wake word score", score=round(float(score), 3), threshold=WAKE_WORD_THRESHOLD)
            if score > WAKE_WORD_THRESHOLD:
                oww.reset()

                # Stop wake-word stream before recording
                oww_stream.stop_stream()
                oww_stream.close()
                pa.terminate()

                try:
                    handle_keyword_detected()
                except Exception as e:
                    logger.warning("Wake response TTS failed, continuing", error=str(e))

                result = None
                try:
                    audio_file = listen()

                    start = time.perf_counter()
                    result = send_for_transcription(
                        audio_file, command_service, stt_provider, validation_handler
                    )
                    end = time.perf_counter()

                    logger.info("Transcription complete", duration_seconds=round(end - start, 2))
                except Exception as e:
                    logger.warning("Command processing failed, resuming listener", error=str(e))
                    print(f"Command failed: {e}")

                # Follow-up listening window (before reopening wake word stream)
                try:
                    _follow_up_loop(result, command_service, stt_provider, validation_handler)
                except Exception as e:
                    logger.warning("Follow-up loop error, resuming wake word", error=str(e))

                # Reopen wake-word stream
                pa = pyaudio.PyAudio()
                oww_stream, needs_resample = _create_oww_stream(pa)
                chunk_size = MIC_CHUNK if needs_resample else OWW_CHUNK
                print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}'")

    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        oww_stream.stop_stream()
        oww_stream.close()
        pa.terminate()
        del oww
