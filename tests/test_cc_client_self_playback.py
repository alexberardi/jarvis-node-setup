"""The node attaches self-playback evidence to the voice command payload.

Slice 2 of the wake-during-music work: ``self_playback`` (and
``self_playback_kind`` when known) ride the /voice/command/stream body the
same way ``pre_wake_speech_seconds`` does — additive fields an older CC
simply ignores (its ``VoiceCommandRequest`` is a default-config Pydantic
model, no ``extra="forbid"``). Note that ``self_playback=False`` IS sent
(the field is gated on ``is not None``, not truthiness) so CC can tell
"node says not self-playing" apart from "old node that doesn't know".
"""
from unittest.mock import MagicMock, patch

from clients.jarvis_command_center_client import JarvisCommandCenterClient

REST = "clients.jarvis_command_center_client.RestClient"


def _resp_200() -> MagicMock:
    """A minimal streamed-audio (200) response so send_command_unified
    returns cleanly after building the payload."""
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


class TestSelfPlaybackInPayload:
    def test_true_with_kind(self):
        payload = _sent_payload(self_playback=True, self_playback_kind="music")
        assert payload["self_playback"] is True
        assert payload["self_playback_kind"] == "music"

    def test_false_is_still_sent(self):
        # Explicit False is informative — only None means "unknown/omit".
        payload = _sent_payload(self_playback=False)
        assert payload["self_playback"] is False
        assert "self_playback_kind" not in payload

    def test_omitted_when_none(self):
        payload = _sent_payload()
        assert "self_playback" not in payload
        assert "self_playback_kind" not in payload

    def test_rides_alongside_existing_fields(self):
        payload = _sent_payload(
            speaker_user_id=7,
            pre_wake_speech_seconds=1.5,
            wake_confidence=0.91,
            self_playback=True,
            self_playback_kind="music",
        )
        assert payload["speaker_user_id"] == 7
        assert payload["pre_wake_speech_seconds"] == 1.5
        assert payload["wake_confidence"] == 0.91
        assert payload["self_playback"] is True
        assert payload["self_playback_kind"] == "music"
        assert payload["voice_command"] == "hi"
        assert payload["conversation_id"] == "conv-1"
