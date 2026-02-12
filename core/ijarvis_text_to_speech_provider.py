from abc import ABC, abstractmethod
import os
from core.platform_audio import platform_audio

# Use relative path for better cross-platform compatibility
CHIME_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sounds", "chime.wav")

class IJarvisTextToSpeechProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the TTS provider (e.g. 'piper', 'espeak')"""
        pass

    @abstractmethod
    def speak(self, include_chime: bool, text: str) -> None:
        """Convert text to speech and play it"""
        pass

    def play_chime(self):
        platform_audio.play_chime(CHIME_PATH)
