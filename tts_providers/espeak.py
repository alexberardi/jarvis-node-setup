import os
import subprocess
import tempfile

from core.ijarvis_text_to_speech_provider import IJarvisTextToSpeechProvider
from core.platform_audio import platform_audio
from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


class EspeakTTS(IJarvisTextToSpeechProvider):
    @property
    def provider_name(self) -> str:
        return "espeak"

    def speak(self, include_chime: bool, text: str) -> None:
        logger.info(f"Speaking '{text}' via espeak")

        if include_chime:
            self.play_chime()
            
        # Generate speech to temporary file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_path = temp_file.name
        
        # Use espeak to generate WAV file
        subprocess.run([
            "espeak", "-a", "20", "-v", "en-uk", "-s", "130", 
            f'"{text}"', "--stdout"
        ], stdout=open(temp_path, 'wb'), check=True)
        
        # Play using platform-agnostic audio
        platform_audio.play_audio_file(temp_path, volume=0.8)
        
        # Clean up temp file
        os.unlink(temp_path)
