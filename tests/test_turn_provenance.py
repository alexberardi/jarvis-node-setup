"""Turn provenance rides the CC voice-command payloads.

CC's not_for_me decision is mode-aware (jarvis-command-center#88): a fresh
wake and a follow-up window get opposite postures. For that to work the
node must say how the mic came to be open:

* wake path   → turn_source="wake" + wake_confidence (the OWW score)
* follow-up   → turn_source="follow_up" + follow_up_iteration (1-based)

Fields are optional and present-only-when-set, exactly like
pre_wake_speech_seconds and affect — an old CC simply ignores them.
"""
from unittest.mock import MagicMock, patch

from clients.jarvis_command_center_client import JarvisCommandCenterClient

REST = "clients.jarvis_command_center_client.RestClient"


def _resp_200() -> MagicMock:
    r = MagicMock()
    r.status_code = 200
    r.headers = {
        "X-Assistant-Message": "",
        "X-Audio-Sample-Rate": "22050",
        "X-Audio-Channels": "1",
        "X-Audio-Sample-Width": "2",
    }
    return r


def _unified_payload(**kwargs) -> dict:
    client = JarvisCommandCenterClient("http://test")
    with patch(REST) as rest:
        rest.post_stream.return_value = _resp_200()
        client.send_command_unified("hi", "conv-1", **kwargs)
        return rest.post_stream.call_args.kwargs["data"]


def _blocking_payload(**kwargs) -> dict:
    client = JarvisCommandCenterClient("http://test")
    with patch(REST) as rest:
        rest.post.return_value = None
        client.send_command("hi", "conv-1", **kwargs)
        return rest.post.call_args.kwargs["data"]


class TestUnifiedPayload:
    def test_wake_provenance_included(self):
        payload = _unified_payload(turn_source="wake", wake_confidence=0.95)
        assert payload["turn_source"] == "wake"
        assert payload["wake_confidence"] == 0.95

    def test_follow_up_provenance_included(self):
        payload = _unified_payload(turn_source="follow_up", follow_up_iteration=2)
        assert payload["turn_source"] == "follow_up"
        assert payload["follow_up_iteration"] == 2

    def test_omitted_when_none(self):
        # Old-CC compatibility: absent fields must not add keys.
        payload = _unified_payload()
        assert "turn_source" not in payload
        assert "wake_confidence" not in payload
        assert "follow_up_iteration" not in payload

    def test_rides_alongside_existing_fields(self):
        payload = _unified_payload(
            speaker_user_id=7,
            pre_wake_speech_seconds=0.0,
            turn_source="wake",
            wake_confidence=0.88,
        )
        assert payload["speaker_user_id"] == 7
        assert payload["pre_wake_speech_seconds"] == 0.0
        assert payload["turn_source"] == "wake"
        assert payload["wake_confidence"] == 0.88


class TestBlockingPayload:
    """continue_conversation posts via send_command — the follow-up loop's
    actual transport. It must carry provenance too, or CC would treat every
    follow-up as an unhinted turn."""

    def test_follow_up_provenance_included(self):
        payload = _blocking_payload(turn_source="follow_up", follow_up_iteration=3)
        assert payload["turn_source"] == "follow_up"
        assert payload["follow_up_iteration"] == 3

    def test_omitted_when_none(self):
        payload = _blocking_payload()
        assert "turn_source" not in payload
        assert "follow_up_iteration" not in payload


class TestServiceForwarding:
    """process_voice_command / continue_conversation must forward provenance
    to the client (and affect, which was previously accepted but dropped)."""

    def _service_with_mock_client(self):
        from utils.command_execution_service import CommandExecutionService

        service = CommandExecutionService.__new__(CommandExecutionService)
        service.client = MagicMock()
        service.client.send_command_unified.return_value = ("error", "stub")
        service.client.send_command.return_value = None
        return service

    def test_process_voice_command_forwards_provenance_and_affect(self):
        service = self._service_with_mock_client()
        affect = {"read": "animated", "arousal": "high", "confidence": 0.6}
        with patch.object(service, "try_pre_route", return_value=None), \
             patch.object(service, "register_tools_for_conversation"), \
             patch.object(service, "_speak_acknowledgment"), \
             patch.object(service, "_handle_error", return_value={}):
            service.process_voice_command(
                "hi",
                register_tools=False,
                conversation_id="conv-1",
                skip_ack=True,
                pre_wake_speech_seconds=0.0,
                affect=affect,
                turn_source="wake",
                wake_confidence=0.91,
            )
        kwargs = service.client.send_command_unified.call_args.kwargs
        assert kwargs["turn_source"] == "wake"
        assert kwargs["wake_confidence"] == 0.91
        # Regression: affect was accepted by process_voice_command but never
        # forwarded — whisper's acoustic read silently died here.
        assert kwargs["affect"] == affect

    def test_continue_conversation_forwards_provenance(self):
        service = self._service_with_mock_client()
        with patch.object(service, "_handle_error", return_value={}):
            service.continue_conversation(
                "conv-1",
                "and tomorrow?",
                turn_source="follow_up",
                follow_up_iteration=2,
            )
        kwargs = service.client.send_command.call_args.kwargs
        assert kwargs["turn_source"] == "follow_up"
        assert kwargs["follow_up_iteration"] == 2
