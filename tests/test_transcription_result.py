"""Tests for TranscriptionResult and transcribe_with_speaker."""
import pytest
from unittest.mock import patch, MagicMock

from core.ijarvis_speech_to_text_provider import TranscriptionResult, IJarvisSpeechToTextProvider


class TestTranscriptionResult:
    def test_defaults(self):
        result = TranscriptionResult(text="hello world")
        assert result.text == "hello world"
        assert result.speaker_user_id is None
        assert result.speaker_confidence == 0.0

    def test_with_speaker(self):
        result = TranscriptionResult(text="hello", speaker_user_id=42, speaker_confidence=0.87)
        assert result.speaker_user_id == 42
        assert result.speaker_confidence == 0.87

    def test_empty_text(self):
        result = TranscriptionResult(text="")
        assert result.text == ""


class _DummyProvider(IJarvisSpeechToTextProvider):
    """Minimal provider that returns fixed text."""

    @property
    def provider_name(self) -> str:
        return "dummy"

    def transcribe(self, audio_path: str) -> str:
        return "test transcription"


class TestTranscribeWithSpeakerDefault:
    def test_default_wraps_transcribe(self):
        provider = _DummyProvider()
        result = provider.transcribe_with_speaker("audio.wav")
        assert isinstance(result, TranscriptionResult)
        assert result.text == "test transcription"
        assert result.speaker_user_id is None
        assert result.speaker_confidence == 0.0


class TestJarvisWhisperClientTranscribeWithSpeaker:
    def test_returns_speaker_data(self):
        from stt_providers.jarvis_whisper_client import JarvisWhisperClient

        client = JarvisWhisperClient()
        with patch.object(client, "_call_whisper", return_value={
            "text": "turn on the lights",
            "speaker": {"user_id": 5, "confidence": 0.92},
        }):
            result = client.transcribe_with_speaker("audio.wav")

        assert result.text == "turn on the lights"
        assert result.speaker_user_id == 5
        assert result.speaker_confidence == 0.92

    def test_no_speaker_in_response(self):
        from stt_providers.jarvis_whisper_client import JarvisWhisperClient

        client = JarvisWhisperClient()
        with patch.object(client, "_call_whisper", return_value={"text": "hello jarvis"}):
            result = client.transcribe_with_speaker("audio.wav")

        assert result.text == "hello jarvis"
        assert result.speaker_user_id is None
        assert result.speaker_confidence == 0.0

    def test_error_returns_empty(self):
        from stt_providers.jarvis_whisper_client import JarvisWhisperClient

        client = JarvisWhisperClient()
        with patch.object(client, "_call_whisper", return_value=None):
            result = client.transcribe_with_speaker("audio.wav")

        assert result.text == ""
        assert result.speaker_user_id is None

    def test_transcribe_still_works(self):
        from stt_providers.jarvis_whisper_client import JarvisWhisperClient

        client = JarvisWhisperClient()
        with patch.object(client, "_call_whisper", return_value={
            "text": "what time is it",
            "speaker": {"user_id": 3, "confidence": 0.75},
        }):
            text = client.transcribe("audio.wav")

        assert text == "what time is it"
