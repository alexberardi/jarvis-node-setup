import unittest
import tempfile
import os
import sys
from unittest.mock import Mock, patch, MagicMock
from typing import Optional

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Mock PyAudio to prevent ALSA errors
with patch('pyaudio.PyAudio'):
    from core.ijarvis_wake_response_provider import IJarvisWakeResponseProvider
    from wake_response_providers.jarvis_tts_wake_response import JarvisTTSWakeResponseProvider
    from wake_response_providers.static_wake_response import StaticWakeResponseProvider
    from core.helpers import get_wake_response_provider


class TestJarvisTTSWakeResponseProvider(unittest.TestCase):
    """Test the JarvisTTS wake response provider"""

    def setUp(self):
        """Set up test fixtures"""
        self.provider = JarvisTTSWakeResponseProvider()

    def test_provider_name(self):
        """Test provider name"""
        self.assertEqual(self.provider.provider_name, "jarvis-tts-api")

    @patch('wake_response_providers.jarvis_tts_wake_response.Config')
    @patch('wake_response_providers.jarvis_tts_wake_response.httpx.post')
    def test_fetch_next_wake_response_success(self, mock_httpx, mock_config):
        """Test successful wake response generation"""
        # Mock config
        mock_config.get_str.return_value = "https://test-tts-api.com"
        
        # Mock HTTP response
        mock_response = Mock()
        mock_response.json.return_value = {"text": "Hello there! How can I help you today?"}
        mock_httpx.return_value = mock_response
        
        result = self.provider.fetch_next_wake_response()
        
        # Verify result
        self.assertEqual(result, "Hello there! How can I help you today?")
        
        # Verify HTTP call
        mock_httpx.assert_called_once_with("https://test-tts-api.com/generate-wake-response", timeout=10.0)

    @patch('wake_response_providers.jarvis_tts_wake_response.Config')
    def test_fetch_next_wake_response_no_url_configured(self, mock_config):
        """Test behavior when URL is not configured"""
        mock_config.get_str.return_value = None
        
        result = self.provider.fetch_next_wake_response()
        
        self.assertIsNone(result)

    @patch('wake_response_providers.jarvis_tts_wake_response.Config')
    @patch('wake_response_providers.jarvis_tts_wake_response.httpx.post')
    def test_fetch_next_wake_response_empty_response(self, mock_httpx, mock_config):
        """Test behavior with empty response from API"""
        mock_config.get_str.return_value = "https://test-tts-api.com"
        
        mock_response = Mock()
        mock_response.json.return_value = {"text": ""}
        mock_httpx.return_value = mock_response
        
        result = self.provider.fetch_next_wake_response()
        
        self.assertIsNone(result)

    @patch('wake_response_providers.jarvis_tts_wake_response.Config')
    @patch('wake_response_providers.jarvis_tts_wake_response.httpx.post')
    def test_fetch_next_wake_response_http_error(self, mock_httpx, mock_config):
        """Test behavior with HTTP error"""
        mock_config.get_str.return_value = "https://test-tts-api.com"
        mock_httpx.side_effect = Exception("HTTP Error")
        
        result = self.provider.fetch_next_wake_response()
        
        self.assertIsNone(result)

    @patch('wake_response_providers.jarvis_tts_wake_response.Config')
    @patch('wake_response_providers.jarvis_tts_wake_response.httpx.post')
    def test_fetch_next_wake_response_missing_text_field(self, mock_httpx, mock_config):
        """Test behavior when response doesn't contain text field"""
        mock_config.get_str.return_value = "https://test-tts-api.com"
        
        mock_response = Mock()
        mock_response.json.return_value = {"status": "ok"}
        mock_httpx.return_value = mock_response
        
        result = self.provider.fetch_next_wake_response()
        
        self.assertIsNone(result)


class TestStaticWakeResponseProvider(unittest.TestCase):
    """Test the static wake response provider"""

    def setUp(self):
        """Set up test fixtures"""
        self.provider = StaticWakeResponseProvider()

    def test_provider_name(self):
        """Test provider name"""
        self.assertEqual(self.provider.provider_name, "static")

    def test_fetch_next_wake_response(self):
        """Test that static provider returns None"""
        result = self.provider.fetch_next_wake_response()
        self.assertIsNone(result)


class TestWakeResponseProviderHelpers(unittest.TestCase):
    """Test the wake response provider helper functions"""

    @patch('core.helpers.Config')
    def test_get_wake_response_provider_not_configured(self, mock_config):
        """Test behavior when no provider is configured"""
        mock_config.get_str.return_value = None
        
        # Clear the cache to ensure fresh test
        get_wake_response_provider.cache_clear()
        
        result = get_wake_response_provider()
        
        self.assertIsNone(result)

    @patch('core.helpers.Config')
    def test_get_wake_response_provider_static(self, mock_config):
        """Test getting static provider"""
        mock_config.get_str.return_value = "static"
        
        # Clear the cache to ensure fresh test
        get_wake_response_provider.cache_clear()
        
        result = get_wake_response_provider()
        
        self.assertIsNotNone(result)
        self.assertEqual(result.provider_name, "static")

    @patch('core.helpers.Config')
    def test_get_wake_response_provider_jarvis_tts(self, mock_config):
        """Test getting jarvis-tts-api provider"""
        mock_config.get_str.return_value = "jarvis-tts-api"
        
        # Clear the cache to ensure fresh test
        get_wake_response_provider.cache_clear()
        
        result = get_wake_response_provider()
        
        self.assertIsNotNone(result)
        self.assertEqual(result.provider_name, "jarvis-tts-api")

    @patch('core.helpers.Config')
    def test_get_wake_response_provider_invalid(self, mock_config):
        """Test behavior with invalid provider name"""
        mock_config.get_str.return_value = "invalid_provider"
        
        # Clear the cache to ensure fresh test
        get_wake_response_provider.cache_clear()
        
        with self.assertRaises(ValueError, msg="Wake response provider 'invalid_provider' not found"):
            get_wake_response_provider()


class TestWakeResponseIntegration(unittest.TestCase):
    """Integration tests for wake response functionality"""

    def setUp(self):
        """Set up test fixtures"""
        self.temp_dir = tempfile.mkdtemp()
        self.wake_file_path = os.path.join(self.temp_dir, "next_wake_response.txt")

    def tearDown(self):
        """Clean up test fixtures"""
        if os.path.exists(self.wake_file_path):
            os.remove(self.wake_file_path)
        if os.path.exists(self.temp_dir):
            os.rmdir(self.temp_dir)

    @patch('scripts.voice_listener.WAKE_FILE', tempfile.mktemp())
    @patch('scripts.voice_listener.get_wake_response_provider')
    def test_fetch_next_wake_response_integration(self, mock_get_provider):
        """Test integration of wake response fetching"""
        # Mock provider
        mock_provider = Mock(spec=IJarvisWakeResponseProvider)
        mock_provider.fetch_next_wake_response.return_value = "Dynamic greeting!"
        mock_get_provider.return_value = mock_provider
        
        # Import and test the function
        from scripts.voice_listener import fetch_next_wake_response
        
        fetch_next_wake_response()
        
        # Verify provider was called
        mock_provider.fetch_next_wake_response.assert_called_once()

    @patch('scripts.voice_listener.WAKE_FILE', tempfile.mktemp())
    @patch('scripts.voice_listener.get_wake_response_provider')
    def test_fetch_next_wake_response_no_provider(self, mock_get_provider):
        """Test integration when no provider is configured"""
        mock_get_provider.return_value = None
        
        # Import and test the function
        from scripts.voice_listener import fetch_next_wake_response
        
        # Should not raise exception
        fetch_next_wake_response()

    @patch('scripts.voice_listener.WAKE_FILE', tempfile.mktemp())
    @patch('scripts.voice_listener.get_wake_response_provider')
    def test_fetch_next_wake_response_provider_exception(self, mock_get_provider):
        """Test integration when provider raises exception"""
        mock_provider = Mock(spec=IJarvisWakeResponseProvider)
        mock_provider.fetch_next_wake_response.side_effect = Exception("Provider error")
        mock_get_provider.return_value = mock_provider
        
        # Import and test the function
        from scripts.voice_listener import fetch_next_wake_response
        
        # Should not raise exception
        fetch_next_wake_response()


if __name__ == '__main__':
    unittest.main() 