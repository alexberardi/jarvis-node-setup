import os
import threading
import time
from pathlib import Path

import numpy as np
import pvporcupine
import pyaudio
from scipy.signal import resample
from jarvis_log_client import JarvisLogger

from core.helpers import get_tts_provider, get_stt_provider, get_wake_response_provider
from scripts.speech_to_text import listen
from utils.config_service import Config
from utils.command_execution_service import CommandExecutionService
from clients.responses.jarvis_command_center import ValidationRequest

logger = JarvisLogger(service="jarvis-node")

CHIME_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sounds", "chime.wav")
WAKE_FILE = Path("/tmp/next_wake_response.txt")


PORCUPINE_KEY = Config.get_str("porcupine_key", "")
MIC_SAMPLE_RATE = Config.get_int("mic_sample_rate", 48000) or 48000
FRAMES_PER_BUFFER = int(MIC_SAMPLE_RATE * 0.032)  # ~32ms chunk

MIC_DEVICE_INDEX = Config.get_int("mic_device_index", 1) or 1
pa = pyaudio.PyAudio()


def create_audio_stream():
    return pa.open(
        rate=MIC_SAMPLE_RATE,
        channels=1,
        format=pyaudio.paInt16,
        input=True,
        frames_per_buffer=FRAMES_PER_BUFFER,
        # input_device_index=MIC_DEVICE_INDEX,
    )


def handle_keyword_detected():
    logger.info("Wake word detected, listening for command")
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


def close_audio(audio_stream):
    audio_stream.stop_stream()
    audio_stream.close()
    pa.terminate()


def send_for_transcription(filename):
    logger.info("Sending audio to transcription server")
    stt_provider = get_stt_provider()
    response = stt_provider.transcribe(filename)

    if response is not None:
        # The response is the transcription text directly
        transcription = response
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
        result = command_service.process_voice_command(transcription, validation_handler)
        command_service.speak_result(result)

        return result
    else:
        tts_provider = get_tts_provider()
        tts_provider.speak(False, "An error occurred during transcription")
        return None


def start_voice_listener(ma_service):
    porcupine = pvporcupine.create(access_key=PORCUPINE_KEY, keywords=["jarvis"])
    pa = pyaudio.PyAudio()
    audio_stream = create_audio_stream()

    logger.info("Waiting for wake word")

    try:
        while True:
            raw_data = audio_stream.read(
                audio_stream._frames_per_buffer, exception_on_overflow=False
            )
            samples = np.frombuffer(raw_data, dtype=np.int16)

            # Resample from 48000 → 16000 Hz
            resampled = resample(samples, porcupine.frame_length).astype(np.int16)

            keyword_index = porcupine.process(resampled.tolist())
            if keyword_index >= 0:
                handle_keyword_detected()

                close_audio(audio_stream)

                audio_file = listen()

                start = time.perf_counter()
                command = send_for_transcription(audio_file)
                end = time.perf_counter()

                logger.info("Transcription complete", duration_seconds=round(end - start, 2))

                # reinitialize porcupine + pyaudio
                pa = pyaudio.PyAudio()
                audio_stream = create_audio_stream()

    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        audio_stream.stop_stream()
        audio_stream.close()
        pa.terminate()
        porcupine.delete()
