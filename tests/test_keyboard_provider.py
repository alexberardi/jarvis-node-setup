"""
Unit tests for KeyboardProvider.

Tests that the keyboard STT provider reads from stdin
and is discoverable through the provider registry.
"""

from unittest.mock import patch

import pytest

from stt_providers.keyboard_provider import KeyboardProvider


@pytest.fixture
def provider():
    return KeyboardProvider()


class TestKeyboardProviderProperties:
    """Test provider metadata."""

    def test_provider_name(self, provider):
        assert provider.provider_name == "keyboard"


class TestKeyboardProviderTranscribe:
    """Test transcribe behavior."""

    @patch("builtins.input", return_value="Hello Jarvis")
    def test_transcribe_returns_user_input(self, mock_input, provider):
        result = provider.transcribe("/fake/audio/path.wav")
        assert result == "Hello Jarvis"

    @patch("builtins.input", return_value="Hello Jarvis")
    def test_transcribe_ignores_audio_path(self, mock_input, provider):
        """Audio path is ignored — keyboard reads from stdin."""
        provider.transcribe("/some/path.wav")
        mock_input.assert_called_once_with("You: ")

    @patch("builtins.input", side_effect=EOFError)
    def test_transcribe_returns_none_on_eof(self, mock_input, provider):
        result = provider.transcribe("/fake/path.wav")
        assert result is None

    @patch("builtins.input", return_value="")
    def test_transcribe_returns_empty_string(self, mock_input, provider):
        result = provider.transcribe("/fake/path.wav")
        assert result == ""


class TestKeyboardProviderDiscovery:
    """Test that the provider is discoverable via get_stt_provider."""

    @patch("core.helpers.Config")
    def test_discoverable_via_get_stt_provider(self, mock_config):
        """KeyboardProvider can be found by the provider registry."""
        mock_config.get_str.return_value = "keyboard"

        # Clear the lru_cache so a fresh lookup happens
        from core.helpers import get_stt_provider
        get_stt_provider.cache_clear()

        provider = get_stt_provider()
        assert provider.provider_name == "keyboard"
        assert isinstance(provider, KeyboardProvider)

        # Clean up cache
        get_stt_provider.cache_clear()
