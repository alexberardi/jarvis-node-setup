"""STT provider that uses command-center's media proxy.

Phase 6 Migration: Direct Whisper calls -> Command-center proxy
- Calls command-center's /api/v0/media/whisper/transcribe endpoint
- Uses node authentication (X-API-Key header)
- Command-center handles app-to-app auth with jarvis-whisper-api
- Context headers (household_id, node_id) passed by command-center for voice recognition
"""

from typing import Any, Dict, Optional

from clients.rest_client import RestClient
from core.ijarvis_speech_to_text_provider import IJarvisSpeechToTextProvider, TranscriptionResult
from jarvis_log_client import JarvisLogger
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


class JarvisWhisperClient(IJarvisSpeechToTextProvider):
    """STT provider that proxies through command-center."""

    @property
    def provider_name(self) -> str:
        return "jarvis-whisper-api"

    def transcribe(self, audio_path: str) -> Optional[str]:
        """Transcribe audio to text.

        Args:
            audio_path: Path to the audio file to transcribe

        Returns:
            Transcribed text, or None on error
        """
        result = self._call_whisper(audio_path)
        if result and isinstance(result, dict):
            return result.get("text", "")
        return None

    def transcribe_with_speaker(self, audio_path: str) -> TranscriptionResult:
        """Transcribe audio and return speaker identity if available.

        Args:
            audio_path: Path to the audio file to transcribe

        Returns:
            TranscriptionResult with text and optional speaker data
        """
        result = self._call_whisper(audio_path)
        if result and isinstance(result, dict):
            text = result.get("text", "")
            speaker = result.get("speaker")
            if speaker and isinstance(speaker, dict):
                return TranscriptionResult(
                    text=text,
                    speaker_user_id=speaker.get("user_id"),
                    speaker_confidence=speaker.get("confidence", 0.0),
                )
            return TranscriptionResult(text=text)
        return TranscriptionResult(text="")

    def _call_whisper(self, audio_path: str) -> Optional[Dict[str, Any]]:
        """Call the whisper transcription endpoint.

        Args:
            audio_path: Path to the audio file to transcribe

        Returns:
            Raw JSON response dict, or None on error
        """
        command_center_url = get_command_center_url()
        if not command_center_url:
            logger.error("command_center_url not configured", context={"provider": "whisper"})
            return None

        url = f"{command_center_url}/api/v0/media/whisper/transcribe"

        with open(audio_path, "rb") as f:
            files: Dict[str, Any] = {"file": (audio_path, f, "audio/wav")}
            response: Optional[Dict[str, Any]] = RestClient.post(
                url,
                files=files,
                timeout=60,
            )

        return response
