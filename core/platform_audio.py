"""
Platform-agnostic audio interface for Jarvis Node.

This module provides a unified interface for audio operations across different platforms.
"""

from typing import List, Dict, Any
from core.platform_abstraction import get_audio_provider


class PlatformAudio:
    """Platform-agnostic audio interface"""

    def __init__(self):
        self.audio_provider = get_audio_provider()

    def play_audio_file(self, file_path: str, volume: float = 1.0) -> bool:
        """Play an audio file using platform-appropriate method"""
        return self.audio_provider.play_audio_file(file_path, volume)

    def play_chime(self, chime_path: str) -> bool:
        """Play a chime sound"""
        return self.audio_provider.play_chime(chime_path)

    def get_audio_devices(self) -> List[Dict[str, Any]]:
        """Get available audio devices"""
        return self.audio_provider.get_audio_devices()

    def play_pcm_stream(
        self,
        pcm_iterator,
        sample_rate: int = 22050,
        channels: int = 1,
        sample_width: int = 2,
    ) -> bool:
        """Play raw PCM audio from an iterator of byte chunks."""
        return self.audio_provider.play_pcm_stream(
            pcm_iterator, sample_rate, channels, sample_width
        )

    def cancel_playback(self) -> bool:
        """Cancel any active audio playback (barge-in)."""
        return self.audio_provider.cancel_playback()

    def reset_cancel(self) -> None:
        """Clear the cancel event so future playback proceeds normally."""
        self.audio_provider.reset_cancel()

    @property
    def is_cancelled(self) -> bool:
        """True if playback was cancelled."""
        return self.audio_provider.is_cancelled


# Global instance
platform_audio = PlatformAudio() 