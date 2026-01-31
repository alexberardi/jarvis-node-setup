"""
Audio Pipeline Client for E2E testing

Provides TTS → Whisper round-trip functionality for full audio pipeline tests.
This client converts text to speech via jarvis-tts, then transcribes it back
via jarvis-whisper-api to verify the audio pipeline works correctly.
"""

import os
import tempfile
from pathlib import Path
from typing import Optional

import requests

from utils.config_loader import Config


class AudioPipelineClient:
    """
    Client for TTS → Whisper audio pipeline (full mode testing)

    Requires:
    - jarvis-tts running (default: http://localhost:8000)
    - jarvis-whisper-api running (default: http://localhost:9999)
    """

    def __init__(
        self,
        tts_url: Optional[str] = None,
        whisper_url: Optional[str] = None,
        save_audio_dir: Optional[str] = None,
    ):
        """
        Initialize the audio pipeline client.

        Args:
            tts_url: URL for jarvis-tts service (default from config or localhost:8000)
            whisper_url: URL for jarvis-whisper-api (default from config or localhost:9999)
            save_audio_dir: Optional directory to save audio files for debugging
        """
        self.tts_url = tts_url or Config.get("jarvis_tts_api_url", "http://localhost:8000")
        self.whisper_url = whisper_url or Config.get("jarvis_whisper_api_url", "http://localhost:9999")
        self.save_audio_dir = save_audio_dir

        if self.save_audio_dir:
            Path(self.save_audio_dir).mkdir(parents=True, exist_ok=True)

    def check_services(self) -> dict[str, bool]:
        """
        Check if TTS and Whisper services are available.

        Returns:
            Dict with service availability: {"tts": bool, "whisper": bool}
        """
        results = {"tts": False, "whisper": False}

        # Check TTS
        try:
            response = requests.get(f"{self.tts_url}/ping", timeout=5)
            results["tts"] = response.status_code == 200
        except requests.RequestException:
            pass

        # Check Whisper
        try:
            response = requests.get(f"{self.whisper_url}/ping", timeout=5)
            results["whisper"] = response.status_code == 200
        except requests.RequestException:
            pass

        return results

    def text_to_speech(self, text: str, save_name: Optional[str] = None) -> Optional[bytes]:
        """
        Convert text to speech audio via jarvis-tts.

        Args:
            text: Text to convert to speech
            save_name: Optional name to save audio file (without extension)

        Returns:
            WAV audio bytes, or None if failed
        """
        try:
            response = requests.post(
                f"{self.tts_url}/speak",
                json={"text": text},
                timeout=30,
            )
            response.raise_for_status()
            audio_bytes = response.content

            # Save audio if directory is set
            if self.save_audio_dir and save_name:
                audio_path = Path(self.save_audio_dir) / f"{save_name}.wav"
                audio_path.write_bytes(audio_bytes)
                print(f"   📁 Saved audio to: {audio_path}")

            return audio_bytes

        except requests.RequestException as e:
            print(f"   ❌ TTS error: {e}")
            return None

    def speech_to_text(self, audio_bytes: bytes) -> Optional[str]:
        """
        Transcribe audio via jarvis-whisper-api.

        Args:
            audio_bytes: WAV audio bytes to transcribe

        Returns:
            Transcribed text, or None if failed
        """
        try:
            # Create a temporary file for the audio
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_bytes)
                tmp_path = tmp.name

            try:
                # Send to whisper API
                with open(tmp_path, "rb") as f:
                    files = {"file": ("audio.wav", f, "audio/wav")}
                    response = requests.post(
                        f"{self.whisper_url}/transcribe",
                        files=files,
                        timeout=60,
                    )
                    response.raise_for_status()
                    result = response.json()
                    return result.get("text", "").strip()
            finally:
                # Clean up temp file
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        except requests.RequestException as e:
            print(f"   ❌ Whisper error: {e}")
            return None

    def full_pipeline(
        self,
        text: str,
        save_name: Optional[str] = None,
    ) -> tuple[Optional[str], Optional[bytes]]:
        """
        Complete TTS → Whisper round-trip.

        Args:
            text: Text to convert and transcribe
            save_name: Optional name to save audio file

        Returns:
            Tuple of (transcribed_text, audio_bytes)
        """
        # Text to speech
        audio_bytes = self.text_to_speech(text, save_name)
        if not audio_bytes:
            return None, None

        # Speech to text
        transcription = self.speech_to_text(audio_bytes)

        return transcription, audio_bytes

    def verify_transcription_accuracy(
        self,
        original_text: str,
        transcribed_text: str,
        min_word_overlap: float = 0.6,
    ) -> bool:
        """
        Verify transcription accuracy by checking word overlap.

        Args:
            original_text: The original text that was spoken
            transcribed_text: The text that whisper transcribed
            min_word_overlap: Minimum fraction of words that must match (default 0.6)

        Returns:
            True if transcription is acceptably accurate
        """
        if not original_text or not transcribed_text:
            return False

        # Normalize and tokenize
        original_words = set(original_text.lower().split())
        transcribed_words = set(transcribed_text.lower().split())

        # Remove common stop words that might be dropped
        stop_words = {"a", "an", "the", "is", "are", "what", "how", "please"}
        original_words = original_words - stop_words
        transcribed_words = transcribed_words - stop_words

        if not original_words:
            return True  # Nothing meaningful to match

        # Calculate overlap
        overlap = len(original_words & transcribed_words)
        overlap_ratio = overlap / len(original_words)

        return overlap_ratio >= min_word_overlap
