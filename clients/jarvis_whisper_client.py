"""Whisper client that uses command-center's media proxy.

Phase 6 Migration: Direct Whisper calls → Command-center proxy
- Calls command-center's /api/v0/media/whisper/transcribe endpoint
- Uses node authentication (X-API-Key header)
"""

from typing import Any, Dict, Optional

from jarvis_log_client import JarvisLogger

from .rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


class JarvisWhisperClient:
    """Whisper client that proxies through command-center."""

    @staticmethod
    def transcribe(audio_path: str) -> Optional[Dict[str, Any]]:
        """Transcribe audio to text via command-center proxy.

        Args:
            audio_path: Path to the audio file to transcribe

        Returns:
            Response dict with "text" key, or None on error
        """
        command_center_url = get_command_center_url()
        if not command_center_url:
            logger.error("command_center_url not configured")
            return None

        url = f"{command_center_url}/api/v0/media/whisper/transcribe"

        with open(audio_path, "rb") as f:
            logger.debug("Transcribing audio via command-center", path=audio_path)
            files = {"file": (audio_path, f, "audio/wav")}
            response = RestClient.post(url, files=files, timeout=60)
            return response
