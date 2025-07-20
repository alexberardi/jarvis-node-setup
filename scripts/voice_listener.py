import numpy as np
import pvporcupine
import pyaudio
from scipy.signal import resample
import time
import threading
from pathlib import Path

from scripts.speech_to_text import listen
from utils.config_service import Config
from core.helpers import get_tts_provider, get_stt_provider, get_wake_response_provider

CHIME_PATH = "/home/pi/projects/jarvis-node-setup/sounds/chime.wav"
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
    print("🟢 Wake word detected! Listening for command...")
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
            print("[wake-response] No wake response provider configured")
            return
            
        response_text = provider.fetch_next_wake_response()
        if response_text:
            WAKE_FILE.write_text(response_text)
            print(f"[wake-response] Stored next wake response: {response_text}")

    except Exception as e:
        print(f"[wake-response] Failed to fetch next greeting: {e}")


def close_audio(audio_stream):
    audio_stream.stop_stream()
    audio_stream.close()
    pa.terminate()


def send_for_transcription(filename):
    print("📡 Sending to transcription server...")
    stt_provider = get_stt_provider()
    response = stt_provider.transcribe(filename)
    
    if response is not None:
        # The response is the transcription text directly
        transcription = response
        print("📝 Transcription:", transcription)
        
        # Process the command through the command execution service
        from utils.command_execution_service import CommandExecutionService
        command_service = CommandExecutionService()
        
        result = command_service.process_voice_command(transcription)
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

    print("👂 Waiting for wake word...")

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
                print(command)
                end = time.perf_counter()

                print(f"⏱️ Transcription took {end - start:.2f} seconds")

                # reinitialize porcupine + pyaudio
                pa = pyaudio.PyAudio()
                audio_stream = create_audio_stream()

    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        audio_stream.stop_stream()
        audio_stream.close()
        pa.terminate()
        porcupine.delete()
