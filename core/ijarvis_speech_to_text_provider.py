from abc import ABC, abstractmethod


class IJarvisSpeechToTextProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the STT provider (e.g. 'jarvis_whisper', 'google') """
        pass

    @abstractmethod
    def transcribe(self, audio_path:str) -> str:
        """Transcribe speech from the given audio path"""
        pass
