import httpx
import tempfile
import subprocess
from typing import Optional
from core.ijarvis_text_to_speech_provider import IJarvisTextToSpeechProvider
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

        subprocess.run(
            f"sox {audio_path} -t wav - vol 0.2 | aplay -D output",
            shell=True,
            check=True
        )
