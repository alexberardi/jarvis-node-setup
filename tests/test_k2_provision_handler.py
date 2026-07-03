"""Tests for the K2 provision MQTT handler — zero-trust nudge + authenticated pull.

The MQTT ``k2/provision`` message is only a nudge (a request_id). The actual key
material is fetched from CC over the node's authenticated (X-API-Key) channel via
``_fetch_k2_from_cc``, so a spoofed broker message cannot overwrite the node's K2
(the settings-sync key). These tests assert the handler never saves a key it
didn't pull from CC.
"""

import json
from unittest.mock import patch

from scripts.mqtt_tts_listener import _handle_k2_provision


def _payload(**fields) -> bytes:
    return json.dumps(fields).encode()


_KEY_OK = {"k2": "dGVzdGtleQ", "kid": "kid-1", "created_at": "2026-07-02T00:00:00"}


class TestK2ProvisionHandler:
    @patch("utils.encryption_utils.save_k2")
    @patch("scripts.mqtt_tts_listener._ack_k2_provision")
    @patch("scripts.mqtt_tts_listener._fetch_k2_from_cc", return_value=_KEY_OK)
    def test_valid_nudge_pulls_key_and_saves(self, mock_fetch, mock_ack, mock_save):
        _handle_k2_provision(_payload(request_id="rid-1"))

        mock_fetch.assert_called_once_with("rid-1")
        mock_save.assert_called_once()
        args = mock_save.call_args.args
        assert args[0] == "dGVzdGtleQ"
        assert args[1] == "kid-1"
        mock_ack.assert_called_once()
        assert mock_ack.call_args.kwargs["success"] is True

    @patch("utils.encryption_utils.save_k2")
    @patch("scripts.mqtt_tts_listener._ack_k2_provision")
    @patch("scripts.mqtt_tts_listener._fetch_k2_from_cc", return_value=None)
    def test_spoofed_nudge_saves_nothing(self, mock_fetch, mock_ack, mock_save):
        """A forged nudge: CC has no pending K2 for this request_id (fetch
        returns None) → the node saves nothing and acks failure."""
        _handle_k2_provision(_payload(request_id="rid-spoof"))

        mock_save.assert_not_called()
        mock_ack.assert_called_once()
        assert mock_ack.call_args.kwargs["success"] is False

    @patch("utils.encryption_utils.save_k2")
    @patch("scripts.mqtt_tts_listener._ack_k2_provision")
    @patch("scripts.mqtt_tts_listener._fetch_k2_from_cc")
    def test_missing_request_id_does_nothing(self, mock_fetch, mock_ack, mock_save):
        _handle_k2_provision(_payload(foo="bar"))

        mock_fetch.assert_not_called()
        mock_save.assert_not_called()
        mock_ack.assert_not_called()

    @patch("utils.encryption_utils.save_k2")
    @patch("scripts.mqtt_tts_listener._ack_k2_provision")
    @patch("scripts.mqtt_tts_listener._fetch_k2_from_cc",
           return_value={"k2": "", "kid": "", "created_at": ""})
    def test_incomplete_pulled_key_not_saved(self, mock_fetch, mock_ack, mock_save):
        _handle_k2_provision(_payload(request_id="rid-x"))

        mock_save.assert_not_called()
        mock_ack.assert_called_once()
        assert mock_ack.call_args.kwargs["success"] is False
