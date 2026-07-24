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

    def test_defaults_have_empty_segments(self):
        result = TranscriptionResult(text="hello")
        assert result.segments == []

    def test_with_segments(self):
        segs = [
            {"t0_ms": 0, "t1_ms": 500, "text": "Hello "},
            {"t0_ms": 600, "t1_ms": 900, "text": "world"},
        ]
        result = TranscriptionResult(text="Hello world", segments=segs)
        assert result.segments == segs


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

    def test_segments_passed_through(self):
        from stt_providers.jarvis_whisper_client import JarvisWhisperClient

        client = JarvisWhisperClient()
        segs = [
            {"t0_ms": 0, "t1_ms": 400, "text": "Hey "},
            {"t0_ms": 450, "t1_ms": 800, "text": "Jarvis"},
        ]
        with patch.object(client, "_call_whisper", return_value={
            "text": "Hey Jarvis",
            "segments": segs,
            "speaker": {"user_id": 1, "confidence": 0.99},
        }):
            result = client.transcribe_with_speaker("audio.wav")

        assert result.segments == segs
        assert result.speaker_user_id == 1

    def test_missing_segments_defaults_to_empty(self):
        from stt_providers.jarvis_whisper_client import JarvisWhisperClient

        client = JarvisWhisperClient()
        with patch.object(client, "_call_whisper", return_value={"text": "ok"}):
            result = client.transcribe_with_speaker("audio.wav")

        assert result.segments == []


class TestAffectPassthrough:
    """The node forwards whisper's opaque `affect` block without interpreting it."""

    def test_result_affect_defaults_none(self):
        assert TranscriptionResult(text="hi").affect is None

    def _client(self):
        from stt_providers.jarvis_whisper_client import JarvisWhisperClient
        return JarvisWhisperClient()

    def test_affect_forwarded_when_present(self):
        client = self._client()
        affect = {"read": "subdued — flat pitch", "arousal": "low", "confidence": 0.72}
        with patch.object(client, "_call_whisper", return_value={
            "text": "how do i get miles to sleep",
            "speaker": {"user_id": 1, "confidence": 0.9},
            "affect": affect,
        }):
            result = client.transcribe_with_speaker("audio.wav")
        assert result.affect == affect

    def test_affect_forwarded_without_speaker(self):
        client = self._client()
        affect = {"read": "animated", "arousal": "high", "confidence": 0.6}
        with patch.object(client, "_call_whisper", return_value={"text": "hi", "affect": affect}):
            result = client.transcribe_with_speaker("audio.wav")
        assert result.affect == affect

    def test_null_affect_becomes_none(self):
        client = self._client()
        with patch.object(client, "_call_whisper", return_value={"text": "hi", "affect": None}):
            result = client.transcribe_with_speaker("audio.wav")
        assert result.affect is None

    def test_malformed_affect_ignored(self):
        # A non-dict affect (bug / older whisper) must never travel downstream.
        client = self._client()
        with patch.object(client, "_call_whisper", return_value={"text": "hi", "affect": "high"}):
            result = client.transcribe_with_speaker("audio.wav")
        assert result.affect is None

    def test_absent_affect_is_none(self):
        client = self._client()
        with patch.object(client, "_call_whisper", return_value={"text": "hi"}):
            result = client.transcribe_with_speaker("audio.wav")
        assert result.affect is None
