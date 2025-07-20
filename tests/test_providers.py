import unittest
import tempfile
import os
import sys
from unittest.mock import Mock, patch, MagicMock, mock_open
from typing import Dict, Any

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from stt_providers.jarvis_whisper_client import JarvisWhisperClient
from tts_providers.espeak import EspeakTTS
from tts_providers.jarvis_tts_api import JarvisTTS
from core.helpers import get_tts_provider, get_stt_provider


class TestJarvisWhisperClient(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        with patch('stt_providers.jarvis_whisper_client.Config') as mock_config:
            mock_config.get_str.return_value = "https://test-whisper-api.com"
            self.whisper_client = JarvisWhisperClient()

    def test_provider_name(self):
        """Test provider name"""
        self.assertEqual(self.whisper_client.provider_name, "jarvis-whisper-api")

    @patch('stt_providers.jarvis_whisper_client.RestClient')
    def test_transcribe_success(self, mock_rest_client):
        """Test successful transcription"""
        # Mock successful response
        mock_response = {"text": "Hello world"}
        mock_rest_client.post.return_value = mock_response
        
        # Create temporary audio file
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(b"fake audio data")
            temp_file_path = temp_file.name
        
        try:
            result = self.whisper_client.transcribe(temp_file_path)
            self.assertEqual(result, "Hello world")
            
            # Verify RestClient was called correctly
            mock_rest_client.post.assert_called_once()
            call_args = mock_rest_client.post.call_args
            self.assertIn("/transcribe", call_args[0][0])
            self.assertIn("files", call_args[1])
        finally:
            os.unlink(temp_file_path)

    @patch('stt_providers.jarvis_whisper_client.RestClient')
    def test_transcribe_no_response(self, mock_rest_client):
        """Test transcription with no response"""
        mock_rest_client.post.return_value = None
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(b"fake audio data")
            temp_file_path = temp_file.name
        
        try:
            result = self.whisper_client.transcribe(temp_file_path)
            self.assertIsNone(result)
        finally:
            os.unlink(temp_file_path)

    @patch('stt_providers.jarvis_whisper_client.RestClient')
    def test_transcribe_invalid_response(self, mock_rest_client):
        """Test transcription with invalid response format"""
        mock_rest_client.post.return_value = "not a dict"
        
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as temp_file:
            temp_file.write(b"fake audio data")
            temp_file_path = temp_file.name
        
        try:
            result = self.whisper_client.transcribe(temp_file_path)
            self.assertIsNone(result)  # Should return None for invalid response
        finally:
            os.unlink(temp_file_path)


class TestEspeakTTS(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        self.espeak_tts = EspeakTTS()

    def test_provider_name(self):
        """Test provider name"""
        self.assertEqual(self.espeak_tts.provider_name, "espeak")

    @patch('tts_providers.espeak.subprocess.run')
    def test_speak_without_chime(self, mock_subprocess):
        """Test speaking without chime"""
        self.espeak_tts.speak(include_chime=False, text="Hello world")
        
        # Verify subprocess was called
        mock_subprocess.assert_called_once()
        call_args = mock_subprocess.call_args
        self.assertIn("espeak", call_args[0][0])
        self.assertIn("Hello world", call_args[0][0])

    @patch('tts_providers.espeak.subprocess.run')
    def test_speak_with_chime(self, mock_subprocess):
        """Test speaking with chime"""
        self.espeak_tts.speak(include_chime=True, text="Hello world")
        
        # Should be called twice: once for chime, once for speech
        self.assertEqual(mock_subprocess.call_count, 2)


class TestJarvisTTS(unittest.TestCase):
    def setUp(self):
        """Set up test fixtures"""
        with patch('tts_providers.jarvis_tts_api.Config') as mock_config:
            mock_config.get_str.return_value = "https://test-tts-api.com"
            self.jarvis_tts = JarvisTTS()

    def test_provider_name(self):
        """Test provider name"""
        self.assertEqual(self.jarvis_tts.provider_name, "jarvis-tts-api")

    @patch('tts_providers.jarvis_tts_api.httpx.post')
    @patch('tts_providers.jarvis_tts_api.subprocess.run')
    @patch('tts_providers.jarvis_tts_api.tempfile.NamedTemporaryFile')
    def test_speak_success(self, mock_tempfile, mock_subprocess, mock_httpx):
        """Test successful TTS"""
        # Mock HTTP response
        mock_response = Mock()
        mock_response.content = b"fake audio data"
        mock_httpx.return_value = mock_response
        
        # Mock temporary file
        mock_temp = Mock()
        mock_temp.name = "/tmp/test.wav"
        mock_tempfile.return_value.__enter__.return_value = mock_temp
        
        # Create a new instance with the mocked config
        with patch('tts_providers.jarvis_tts_api.Config') as mock_config:
            mock_config.get_str.return_value = "https://test-tts-api.com"
            jarvis_tts = JarvisTTS()
            
            jarvis_tts.speak(include_chime=False, text="Hello world")
            
            # Verify HTTP request was made
            mock_httpx.assert_called_once_with(
                "https://test-tts-api.com/speak",
                json={"text": "Hello world"},
                timeout=30.0
            )
            
            # Verify subprocess was called
            mock_subprocess.assert_called_once()

    @patch('tts_providers.jarvis_tts_api.httpx.post')
    def test_speak_no_url_configured(self, mock_httpx):
        """Test TTS with no URL configured"""
        with patch('tts_providers.jarvis_tts_api.Config') as mock_config:
            mock_config.get_str.return_value = None
            
            with self.assertRaises(ValueError, msg="jarvis_tts_api_url not configured"):
                self.jarvis_tts.speak(include_chime=False, text="Hello world")

    @patch('tts_providers.jarvis_tts_api.httpx.post')
    def test_speak_http_error(self, mock_httpx):
        """Test TTS with HTTP error"""
        # Mock HTTP error
        mock_httpx.side_effect = Exception("HTTP Error")
        
        with self.assertRaises(Exception):
            self.jarvis_tts.speak(include_chime=False, text="Hello world")


class TestProviderHelpers(unittest.TestCase):
    @patch('core.helpers.Config')
    def test_get_tts_provider_not_configured(self, mock_config):
        """Test TTS provider helper with no provider configured"""
        mock_config.get_str.return_value = None
        
        with self.assertRaises(ValueError, msg="TTS provider not configured"):
            get_tts_provider()

    @patch('core.helpers.Config')
    def test_get_stt_provider_not_configured(self, mock_config):
        """Test STT provider helper with no provider configured"""
        mock_config.get_str.return_value = None
        
        with self.assertRaises(ValueError, msg="STT provider not configured"):
            get_stt_provider()

    @patch('core.helpers.Config')
    def test_get_tts_provider_invalid_provider(self, mock_config):
        """Test TTS provider helper with invalid provider name"""
        mock_config.get_str.return_value = "invalid_provider"
        
        with self.assertRaises(ValueError, msg="TTS provider 'invalid_provider' not found"):
            get_tts_provider()

    @patch('core.helpers.Config')
    def test_get_stt_provider_invalid_provider(self, mock_config):
        """Test STT provider helper with invalid provider name"""
        mock_config.get_str.return_value = "invalid_provider"
        
        with self.assertRaises(ValueError, msg="STT provider 'invalid_provider' not found"):
            get_stt_provider()


if __name__ == '__main__':
    unittest.main() 