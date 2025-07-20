from abc import ABC, abstractmethod
import os
import subprocess

PATH_TO_PROJECT = "~/projects/jarvis-node-setup"
CHIME_PATH = os.path.expanduser(f"{PATH_TO_PROJECT}/sounds/chime.wav")

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
        subprocess.run(["aplay", CHIME_PATH])
