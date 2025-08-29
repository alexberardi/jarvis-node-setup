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


# Global instance
platform_audio = PlatformAudio() 