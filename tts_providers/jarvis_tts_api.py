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
from jarvis_log_client import JarvisLogger
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


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
        logger.info(f"Speaking '{text}' via command-center TTS proxy")

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
        platform_audio.play_audio_file(audio_path)

    def speak_stream(self, text: str) -> bool:
        """Stream TTS audio with low-latency playback.

        Uses the streaming TTS endpoint to play audio as it's generated,
        avoiding the overhead of buffering the entire WAV file.

        Args:
            text: The text to speak

        Returns:
            True if playback succeeded
        """
        command_center_url = get_command_center_url()
        if not command_center_url:
            logger.error("command_center_url not configured for streaming TTS")
            return False

        url = f"{command_center_url}/api/v0/media/tts/speak/stream"
        response = RestClient.post_stream(url, data={"text": text}, timeout=60)

        if not response:
            logger.warning("Streaming TTS failed, falling back to blocking TTS")
            return False

        sample_rate = int(response.headers.get("X-Audio-Sample-Rate", "22050"))
        channels = int(response.headers.get("X-Audio-Channels", "1"))
        sample_width = int(response.headers.get("X-Audio-Sample-Width", "2"))

        return platform_audio.play_pcm_stream(
            response.iter_content(chunk_size=4096),
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
        )
