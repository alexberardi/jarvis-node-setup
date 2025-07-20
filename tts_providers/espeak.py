import subprocess
from typing import Optional
from utils.config_service import Config
from core.ijarvis_text_to_speech_provider import IJarvisTextToSpeechProvider


class EspeakTTS(IJarvisTextToSpeechProvider):
    @property
    def provider_name(self) -> str:
        return "espeak"

    def speak(self, include_chime: bool, text: str) -> None:
        print(f"Speaking {text}")

        if include_chime:
            self.play_chime()
            
        subprocess.run(
            f'espeak -a 20 -v en-uk -s 130 "{text}" --stdout | aplay -r 44100 -D output',
            shell=True,
        )
