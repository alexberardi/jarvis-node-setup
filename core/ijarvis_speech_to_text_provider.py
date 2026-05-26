from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class TranscriptionResult:
    """Result from speech-to-text transcription, optionally including speaker identity."""
    text: str
    speaker_user_id: int | None = None
    speaker_confidence: float = 0.0


class IJarvisSpeechToTextProvider(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the STT provider (e.g. 'jarvis_whisper', 'google') """
        pass

    @abstractmethod
    def transcribe(self, audio_path: str) -> Optional[str]:
        """Transcribe speech from the given audio path"""
        pass

    def transcribe_with_speaker(
        self,
        audio_path: str,
        *,
        speaker_audio_path: Optional[str] = None,
    ) -> TranscriptionResult:
        """Transcribe audio and return speaker identity if available.

        ``speaker_audio_path`` (optional) lets callers send a separate
        audio file for the speaker pass while ``audio_path`` is used for
        transcription. The wake-word concat path uses this to feed ECAPA
        a longer clip (wake-word + command) without polluting Whisper's
        text output with the wake phrase.

        Default implementation wraps transcribe() with no speaker data
        and ignores ``speaker_audio_path``. Providers that support
        speaker identification should override this.
        """
        text = self.transcribe(audio_path)
        return TranscriptionResult(text=text or "")
