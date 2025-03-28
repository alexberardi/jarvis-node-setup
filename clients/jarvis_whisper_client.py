import os
from .rest_client import RestClient
from utils.config_loader import Config


class JarvisWhisperClient:
    BASE_URL = Config.get("jarvis_whisper_api_url", "")

    @staticmethod
    def transcribe(audio_path: str) -> str:
        with open(audio_path, "rb") as f:
            print(audio_path)
            files = {"file": (audio_path, f, "audio/wav")}
            response = RestClient.post(
                f"{JarvisWhisperClient.BASE_URL}/transcribe", files=files
            )
            return response
