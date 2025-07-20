from typing import Optional, Dict, Any
from clients.rest_client import RestClient
from utils.config_service import Config
from core.ijarvis_speech_to_text_provider import IJarvisSpeechToTextProvider


class JarvisWhisperClient(IJarvisSpeechToTextProvider):
    BASE_URL: str = Config.get_str("jarvis_whisper_api_url", "") or ""

    @property
    def provider_name(self) -> str:
        return "jarvis-whisper-api"

    def transcribe(self, audio_path: str) -> Optional[str]:
        with open(audio_path, "rb") as f:
            files: Dict[str, Any] = {"file": (audio_path, f, "audio/wav")}
            response: Optional[Dict[str, Any]] = RestClient.post(
                f"{JarvisWhisperClient.BASE_URL}/transcribe", files=files
            )
            if response and isinstance(response, dict):
                return response.get("text", "")
            return None
