import subprocess
import tempfile
from typing import Optional

import httpx

from core.ijarvis_text_to_speech_provider import IJarvisTextToSpeechProvider
from core.platform_audio import platform_audio
from utils.config_service import Config


class JarvisTTS(IJarvisTextToSpeechProvider):
    @property
    def provider_name(self) -> str:
        return "jarvis-tts-api"

    def speak(self, include_chime: bool, text: str) -> None:
        print(f"Speaking {text} from JarvisTTS")
        
        tts_url = Config.get_str("jarvis_tts_api_url")
        if not tts_url:
            raise ValueError("jarvis_tts_api_url not configured")
        
        response: httpx.Response = httpx.post(
            tts_url + "/speak",
            json={"text": text},
            timeout=30.0
        )
        response.raise_for_status()

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(response.content)
            audio_path: str = f.name

        if include_chime:
            self.play_chime()

        # Use platform-agnostic audio playback
        platform_audio.play_audio_file(audio_path, volume=0.2)
