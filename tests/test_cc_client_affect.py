"""The node attaches whisper's acoustic-affect block to the voice command payload.

Phase B of acoustic mood detection: affect rides the /voice/command/stream body
exactly like pre_wake_speech_seconds — present only when non-None.
"""
from unittest.mock import MagicMock, patch

from clients.jarvis_command_center_client import JarvisCommandCenterClient

REST = "clients.jarvis_command_center_client.RestClient"


def _resp_200() -> MagicMock:
    """A minimal streamed-audio (200) response so send_command_unified returns
    cleanly after building the payload."""
    r = MagicMock()
    r.status_code = 200
    r.headers = {
        "X-Assistant-Message": "",
        "X-Audio-Sample-Rate": "22050",
        "X-Audio-Channels": "1",
        "X-Audio-Sample-Width": "2",
    }
    return r


def _sent_payload(**kwargs) -> dict:
    client = JarvisCommandCenterClient("http://test")
    with patch(REST) as rest:
        rest.post_stream.return_value = _resp_200()
        client.send_command_unified("hi", "conv-1", **kwargs)
        return rest.post_stream.call_args.kwargs["data"]


class TestAffectInPayload:
    def test_affect_included_when_present(self):
        affect = {"read": "subdued — flat pitch", "arousal": "low", "confidence": 0.72}
        assert _sent_payload(affect=affect)["affect"] == affect

    def test_affect_omitted_when_none(self):
        # Absent affect must not add the key (mirrors speaker_user_id / pre_wake).
        assert "affect" not in _sent_payload()

    def test_affect_rides_alongside_pre_wake_and_speaker(self):
        affect = {"read": "animated", "arousal": "high", "confidence": 0.6}
        payload = _sent_payload(
            speaker_user_id=7, pre_wake_speech_seconds=1.5, affect=affect,
        )
        assert payload["speaker_user_id"] == 7
        assert payload["pre_wake_speech_seconds"] == 1.5
        assert payload["affect"] == affect
        assert payload["voice_command"] == "hi"
        assert payload["conversation_id"] == "conv-1"
