import os
import threading
import time
from pathlib import Path

import numpy as np
import openwakeword
from openwakeword.model import Model as OWWModel
import pyaudio
from jarvis_log_client import JarvisLogger

from core.helpers import get_tts_provider, get_stt_provider, get_wake_response_provider
from scripts.speech_to_text import listen
from utils.config_service import Config
from utils.command_execution_service import CommandExecutionService
from clients.responses.jarvis_command_center import ValidationRequest

logger = JarvisLogger(service="jarvis-node")

CHIME_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sounds", "chime.wav")
WAKE_FILE = Path("/tmp/next_wake_response.txt")

WAKE_WORD_MODEL = Config.get_str("wake_word_model", "hey_jarvis") or "hey_jarvis"
WAKE_WORD_THRESHOLD = Config.get_float("wake_word_threshold", 0.5)

# openWakeWord needs 16 kHz audio in 1280-sample (80 ms) chunks
OWW_RATE = 16000
OWW_CHUNK = 1280

_mic_index_str: str | None = Config.get_str("mic_device_index")
MIC_DEVICE_INDEX: int | None = int(_mic_index_str) if _mic_index_str is not None else None


def handle_keyword_detected():
    logger.info("Wake word detected, listening for command")
    print("Wake word detected! Listening...")
    tts_provider = get_tts_provider()
    if WAKE_FILE.exists():
        wake_text = WAKE_FILE.read_text().strip()
        WAKE_FILE.unlink()  # clear it after use
    else:
        wake_text = "Yes?"

    tts_provider.speak(False, wake_text)

    # Fetch the next wake response in the background if provider is configured
    threading.Thread(target=fetch_next_wake_response, daemon=True).start()


def fetch_next_wake_response():
    """Fetch the next wake response using the configured provider"""
    try:
        provider = get_wake_response_provider()
        if not provider:
            logger.debug("No wake response provider configured")
            return

        response_text = provider.fetch_next_wake_response()
        if response_text:
            WAKE_FILE.write_text(response_text)
            logger.debug("Stored next wake response", response=response_text)

    except Exception as e:
        logger.error("Failed to fetch next greeting", error=str(e))


def send_for_transcription(filename):
    logger.info("Sending audio to transcription server")
    stt_provider = get_stt_provider()
    result = stt_provider.transcribe_with_speaker(filename)

    if result.text:
        transcription = result.text
        speaker_user_id = result.speaker_user_id
        if speaker_user_id:
            logger.info("Transcription received", text=transcription,
                        speaker_user_id=speaker_user_id, speaker_confidence=result.speaker_confidence)
        else:
            logger.info("Transcription received", text=transcription)

        # Process the command through the command execution service
        command_service = CommandExecutionService()

        # Define validation handler that prompts user and re-listens
        def validation_handler(validation: ValidationRequest) -> str:
            """Handle validation by prompting user and capturing their response"""
            tts_provider = get_tts_provider()

            # Speak the validation question
            question = validation.question
            if validation.options:
                # If there are options, include them in the question
                options_text = ", ".join(validation.options)
                question = f"{question} Your options are: {options_text}"

            logger.info("Asking validation question", question=question)
            tts_provider.speak(False, question)

            # Listen for user's response
            logger.debug("Listening for validation response")
            validation_audio = listen()

            # Transcribe the response
            validation_transcription = stt_provider.transcribe(validation_audio)

            if validation_transcription:
                logger.info("User validation response", response=validation_transcription)
                return validation_transcription
            else:
                logger.warning("Failed to transcribe validation response")
                return "I didn't catch that, sorry."

        # Process with validation handler
        result = command_service.process_voice_command(
            transcription, validation_handler, speaker_user_id=speaker_user_id
        )
        command_service.speak_result(result)

        return result
    else:
        tts_provider = get_tts_provider()
        tts_provider.speak(False, "An error occurred during transcription")
        return None


def _start_keyboard_listener() -> None:
    """Fallback listener: press Enter to trigger a command (no wake word)."""
    logger.info("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")
    print("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")
    try:
        while True:
            input()  # block until Enter
            try:
                handle_keyword_detected()
            except Exception as e:
                logger.warning("Wake response TTS failed, continuing", error=str(e))

            audio_file = listen()

            start = time.perf_counter()
            send_for_transcription(audio_file)
            end = time.perf_counter()

            logger.info("Transcription complete", duration_seconds=round(end - start, 2))
            logger.info("Press Enter to speak another command")
    except KeyboardInterrupt:
        logger.info("Stopping voice listener")


def _create_oww_stream(pa_instance: pyaudio.PyAudio):
    """Open a 16 kHz mono stream sized for openWakeWord (1280 samples = 80 ms)."""
    kwargs: dict = dict(
        rate=OWW_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=OWW_CHUNK,
    )
    if MIC_DEVICE_INDEX is not None:
        kwargs["input_device_index"] = MIC_DEVICE_INDEX
    return pa_instance.open(**kwargs)


def start_voice_listener(ma_service):
    # Download model if needed and initialise openWakeWord
    try:
        openwakeword.utils.download_models(model_names=[WAKE_WORD_MODEL])
        oww = OWWModel(wakeword_models=[WAKE_WORD_MODEL], inference_framework="onnx")
    except Exception as e:
        logger.warning("openWakeWord init failed, falling back to keyboard trigger", error=str(e))
        _start_keyboard_listener()
        return

    pa = pyaudio.PyAudio()
    oww_stream = _create_oww_stream(pa)

    logger.info("Waiting for wake word", model=WAKE_WORD_MODEL, threshold=WAKE_WORD_THRESHOLD)
    print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}' (threshold={WAKE_WORD_THRESHOLD})")

    try:
        while True:
            raw_data = oww_stream.read(OWW_CHUNK, exception_on_overflow=False)
            samples = np.frombuffer(raw_data, dtype=np.int16)

            predictions = oww.predict(samples)
            if predictions.get(WAKE_WORD_MODEL, 0) > WAKE_WORD_THRESHOLD:
                oww.reset()

                # Stop wake-word stream before recording
                oww_stream.stop_stream()
                oww_stream.close()
                pa.terminate()

                try:
                    handle_keyword_detected()
                except Exception as e:
                    logger.warning("Wake response TTS failed, continuing", error=str(e))

                try:
                    audio_file = listen()

                    start = time.perf_counter()
                    send_for_transcription(audio_file)
                    end = time.perf_counter()

                    logger.info("Transcription complete", duration_seconds=round(end - start, 2))
                except Exception as e:
                    logger.warning("Command processing failed, resuming listener", error=str(e))
                    print(f"Command failed: {e}")

                # Reopen wake-word stream
                pa = pyaudio.PyAudio()
                oww_stream = _create_oww_stream(pa)
                print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}'")

    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        oww_stream.stop_stream()
        oww_stream.close()
        pa.terminate()
        del oww
