"""STT provider that reads text from keyboard input instead of audio.

Used for development/testing without audio hardware.
Activate via config: "stt_provider": "keyboard"
"""

from typing import Optional

from core.ijarvis_speech_to_text_provider import IJarvisSpeechToTextProvider


class KeyboardProvider(IJarvisSpeechToTextProvider):
    """STT provider that prompts for text input instead of transcribing audio."""

    @property
    def provider_name(self) -> str:
        return "keyboard"

    def transcribe(self, audio_path: str) -> Optional[str]:
        """Ignore audio_path and read from stdin instead.

        Args:
            audio_path: Ignored - keyboard provider does not use audio files

        Returns:
            Text entered by the user, or None on EOF
        """
        try:
            return input("You: ")
        except EOFError:
            return None
