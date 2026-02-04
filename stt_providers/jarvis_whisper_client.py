"""STT provider that uses command-center's media proxy.

Phase 6 Migration: Direct Whisper calls → Command-center proxy
- Calls command-center's /api/v0/media/whisper/transcribe endpoint
- Uses node authentication (X-API-Key header)
- Command-center handles app-to-app auth with jarvis-whisper-api
- Context headers (household_id, node_id) passed by command-center for voice recognition
"""

from typing import Any, Dict, Optional

from clients.rest_client import RestClient
from core.ijarvis_speech_to_text_provider import IJarvisSpeechToTextProvider
from utils.service_discovery import get_command_center_url


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
        command_center_url = get_command_center_url()
        if not command_center_url:
            print("[whisper] command_center_url not configured")
            return None

        # Call command-center's Whisper proxy endpoint
        url = f"{command_center_url}/api/v0/media/whisper/transcribe"

        with open(audio_path, "rb") as f:
            files: Dict[str, Any] = {"file": (audio_path, f, "audio/wav")}
            response: Optional[Dict[str, Any]] = RestClient.post(
                url,
                files=files,
                timeout=60,
            )

        if response and isinstance(response, dict):
            return response.get("text", "")
        return None
