"""Tests for NodeSignalsBackend — the SDK JarvisSignals → command-center bridge."""

from unittest.mock import MagicMock, patch

import pytest

import jarvis_command_sdk.signals as sdk_signals
from jarvis_command_sdk import JarvisSignals

import services.signals_backend as signals_backend
from services.signals_backend import NodeSignalsBackend, init_signals_backend


_CC_URL = "http://cc:7703"


@pytest.fixture(autouse=True)
def _quiet_logger():
    with patch.object(signals_backend, "logger", MagicMock()):
        yield


@pytest.fixture(autouse=True)
def _reset_sdk_backend():
    prior = sdk_signals.get_signals_backend()
    yield
    sdk_signals._backend = prior


@pytest.fixture
def backend() -> NodeSignalsBackend:
    return NodeSignalsBackend()


def _payload() -> dict:
    return {"signal": {"kind": "presence.seen", "source_key": "presence:1"}, "data": None}


class TestEmitSignal:
    def test_ok_posts_to_signals_endpoint(self, backend: NodeSignalsBackend):
        with patch(
            "utils.service_discovery.get_command_center_url", return_value=_CC_URL
        ), patch(
            "clients.rest_client.RestClient.post",
            return_value={"signal_id": 1, "mode": "open", "proposed": False},
        ) as mock_post:
            result = backend.emit_signal(_payload())
        assert result == "ok"
        args, kwargs = mock_post.call_args
        assert args[0].endswith("/api/v0/signals")
        assert kwargs["data"]["signal"]["kind"] == "presence.seen"

    def test_http_error_on_none(self, backend: NodeSignalsBackend):
        with patch(
            "utils.service_discovery.get_command_center_url", return_value=_CC_URL
        ), patch("clients.rest_client.RestClient.post", return_value=None):
            assert backend.emit_signal(_payload()) == "http_error"

    def test_http_error_on_raise(self, backend: NodeSignalsBackend):
        with patch(
            "utils.service_discovery.get_command_center_url", return_value=_CC_URL
        ), patch("clients.rest_client.RestClient.post", side_effect=RuntimeError("boom")):
            assert backend.emit_signal(_payload()) == "http_error"

    def test_no_cc_url(self, backend: NodeSignalsBackend):
        with patch(
            "utils.service_discovery.get_command_center_url", return_value=""
        ), patch("clients.rest_client.RestClient.post") as mock_post:
            assert backend.emit_signal(_payload()) == "no_cc_url"
            mock_post.assert_not_called()


class TestFacadeEndToEnd:
    def test_emit_through_facade_reaches_endpoint(self):
        init_signals_backend()
        with patch(
            "utils.service_discovery.get_command_center_url", return_value=_CC_URL
        ), patch(
            "clients.rest_client.RestClient.post", return_value={"signal_id": 1}
        ) as mock_post:
            tag = JarvisSignals("presence_agent").emit(
                kind="presence.seen", source_key="presence:1", summary="Alex is home",
                facts={"user": "alex"}, ttl_seconds=900,
            )
        assert tag == "ok"
        args, kwargs = mock_post.call_args
        assert args[0].endswith("/api/v0/signals")
        assert kwargs["data"]["signal"]["kind"] == "presence.seen"
        assert kwargs["data"]["signal"]["source_agent"] == "presence_agent"
        assert kwargs["data"]["data"] == {"user": "alex"}
