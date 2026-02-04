"""TTS provider that uses command-center's media proxy.

Phase 6 Migration: Direct TTS calls → Command-center proxy
- Calls command-center's /api/v0/media/tts/speak endpoint
- Uses node authentication (X-API-Key header)
- Command-center handles app-to-app auth with jarvis-tts
"""

import tempfile
from typing import Optional

from clients.rest_client import RestClient
from core.ijarvis_text_to_speech_provider import IJarvisTextToSpeechProvider
from core.platform_audio import platform_audio
from utils.service_discovery import get_command_center_url


class JarvisTTS(IJarvisTextToSpeechProvider):
    """TTS provider that proxies through command-center."""

    @property
    def provider_name(self) -> str:
        return "jarvis-tts-api"

    def speak(self, include_chime: bool, text: str) -> None:
        """Convert text to speech and play it.

        Args:
            include_chime: Whether to play a chime before speaking
            text: The text to speak
        """
        print(f"Speaking '{text}' via command-center TTS proxy")

        command_center_url = get_command_center_url()
        if not command_center_url:
            raise ValueError("command_center_url not configured")

        # Call command-center's TTS proxy endpoint
        url = f"{command_center_url}/api/v0/media/tts/speak"
        audio_bytes: Optional[bytes] = RestClient.post_binary(
            url,
            data={"text": text},
            timeout=30,
        )

        if not audio_bytes:
            raise RuntimeError("Failed to get audio from TTS service")

        # Write to temp file and play
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(audio_bytes)
            audio_path: str = f.name

        if include_chime:
            self.play_chime()

        # Use platform-agnostic audio playback
        platform_audio.play_audio_file(audio_path, volume=0.2)
