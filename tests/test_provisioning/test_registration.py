"""
Unit tests for provisioning registration with token-based auth.
"""

from unittest.mock import patch, MagicMock

import httpx
import pytest

from provisioning.registration import register_with_command_center


class TestRegisterWithCommandCenter:
    """Test token-based registration with command center."""

    def test_posts_to_nodes_register_endpoint(self):
        """URL must be /api/v0/nodes/register."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "key-abc",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
                room="kitchen",
            )

            called_url = mock_client.post.call_args[1].get("url") or mock_client.post.call_args[0][0]
            assert called_url == "http://10.0.0.1:7703/api/v0/nodes/register"

    def test_sends_node_id_and_token_in_payload(self):
        """Payload must contain node_id and provisioning_token."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "key-abc",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
                room="kitchen",
            )

            payload = mock_client.post.call_args[1].get("json") or mock_client.post.call_args[1]
            assert payload["node_id"] == "node-uuid-123"
            assert payload["provisioning_token"] == "tok_abc"

    def test_no_x_api_key_header(self):
        """Must NOT send X-API-Key header."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "key-abc",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
            )

            # Check no X-API-Key in headers
            call_kwargs = mock_client.post.call_args[1]
            headers = call_kwargs.get("headers", {})
            assert "X-API-Key" not in headers

    def test_room_omitted_when_none(self):
        """Room should not be in payload when None."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "key-abc",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
                room=None,
            )

            payload = mock_client.post.call_args[1]["json"]
            assert "room" not in payload

    def test_room_included_when_provided(self):
        """Room should be in payload when provided."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "key-abc",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
                room="kitchen",
            )

            payload = mock_client.post.call_args[1]["json"]
            assert payload["room"] == "kitchen"

    def test_returns_node_id_and_node_key_on_success(self):
        """Should return dict with node_id and node_key on 200/201."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "secret-key-xyz",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
            )

            assert result is not None
            assert result["node_id"] == "node-uuid-123"
            assert result["node_key"] == "secret-key-xyz"

    def test_returns_none_on_401(self):
        """Should return None on 401 Unauthorized."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_expired",
            )

            assert result is None

    def test_returns_none_on_400(self):
        """Should return None on 400 Bad Request."""
        mock_response = MagicMock()
        mock_response.status_code = 400

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
            )

            assert result is None

    def test_returns_none_on_network_error(self):
        """Should return None after exhausting retries on network errors."""
        with patch("provisioning.registration.httpx.Client") as mock_client_cls, \
             patch("provisioning.registration.time.sleep") as mock_sleep:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_client_cls.return_value = mock_client

            result = register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
            )

            assert result is None
            from provisioning.registration import _CONNECT_RETRIES
            assert mock_client.post.call_count == _CONNECT_RETRIES
            assert mock_sleep.call_count == _CONNECT_RETRIES - 1

    def test_retries_transient_network_error_then_succeeds(self):
        """Registration fires seconds after the WiFi join, before DHCP/routes
        settle — a connect-level failure there is transient and must be
        retried, not treated as terminal (regression: '[Errno 101] Network is
        unreachable' one-shot failure stranded a fresh node at ERROR 70%)."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "key-abc",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls, \
             patch("provisioning.registration.time.sleep") as mock_sleep:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = [
                httpx.ConnectError("[Errno 101] Network is unreachable"),
                httpx.ConnectError("[Errno -3] Temporary failure in name resolution"),
                mock_response,
            ]
            mock_client_cls.return_value = mock_client

            result = register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
            )

            assert result is not None
            assert result["node_key"] == "key-abc"
            assert mock_client.post.call_count == 3
            assert mock_sleep.call_count == 2

    def test_http_rejection_does_not_retry(self):
        """An HTTP response (e.g. 401 expired token) means the network is fine
        — retrying can't help and would just burn the provisioning window."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("provisioning.registration.httpx.Client") as mock_client_cls, \
             patch("provisioning.registration.time.sleep") as mock_sleep:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            result = register_with_command_center(
                command_center_url="http://10.0.0.1:7703",
                node_id="node-uuid-123",
                provisioning_token="tok_expired",
            )

            assert result is None
            assert mock_client.post.call_count == 1
            mock_sleep.assert_not_called()

    def test_url_trailing_slash_stripped(self):
        """Trailing slash on command_center_url should be stripped."""
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "node-uuid-123",
            "node_key": "key-abc",
        }

        with patch("provisioning.registration.httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_client_cls.return_value = mock_client

            register_with_command_center(
                command_center_url="http://10.0.0.1:7703/",
                node_id="node-uuid-123",
                provisioning_token="tok_abc",
            )

            called_url = mock_client.post.call_args[0][0]
            assert called_url == "http://10.0.0.1:7703/api/v0/nodes/register"
            assert "//" not in called_url.split("://")[1]


def _client_returning(*outcomes):
    """Patch httpx.Client so successive posts return/raise ``outcomes``."""
    mock_client = MagicMock()
    mock_client.__enter__ = MagicMock(return_value=mock_client)
    mock_client.__exit__ = MagicMock(return_value=False)
    mock_client.post.side_effect = list(outcomes)
    return mock_client


def _resp(status: int, body=None, text: str = "") -> MagicMock:
    r = MagicMock()
    r.status_code = status
    if isinstance(body, Exception):
        r.json.side_effect = body
    else:
        r.json.return_value = body
    r.text = text
    return r


class TestRegisterNodeResult:
    """register_node reports *why* registration failed so the app can show it."""

    def _call(self, mock_client):
        from provisioning.registration import register_node

        with patch("provisioning.registration.httpx.Client", return_value=mock_client), \
             patch("provisioning.registration.time.sleep") as mock_sleep:
            result = register_node("http://cc:7703", "node-1", "tok-SECRET-123", "kitchen")
        return result, mock_sleep

    def test_success(self):
        client = _client_returning(_resp(201, {"node_id": "node-1", "node_key": "k"}))
        result, _ = self._call(client)
        assert result.ok and result.node_key == "k" and result.reason == ""

    def test_401_is_token_rejected_with_cc_detail_and_no_retry(self):
        client = _client_returning(
            _resp(401, {"detail": "Invalid or expired provisioning token"})
        )
        result, sleep = self._call(client)
        assert not result.ok
        assert result.error_kind == "token_rejected"
        assert result.status_code == 401
        assert result.detail == "Invalid or expired provisioning token"
        assert result.reason == (
            "Command center rejected registration (HTTP 401): "
            "Invalid or expired provisioning token"
        )
        assert client.post.call_count == 1
        sleep.assert_not_called()

    def test_other_4xx_is_rejected_without_retry(self):
        client = _client_returning(_resp(409, ValueError("no json"), text="already registered"))
        result, sleep = self._call(client)
        assert result.error_kind == "rejected"
        assert result.detail == "already registered"
        assert client.post.call_count == 1

    def test_5xx_is_retried_then_succeeds(self):
        client = _client_returning(
            _resp(503, {"detail": "starting"}),
            _resp(201, {"node_id": "node-1", "node_key": "k"}),
        )
        result, sleep = self._call(client)
        assert result.ok
        assert client.post.call_count == 2
        assert sleep.call_count == 1

    def test_5xx_gives_up_after_retries(self):
        from provisioning.registration import _SERVER_ERROR_RETRIES

        client = _client_returning(*[_resp(500, {"detail": "db down"})] * _SERVER_ERROR_RETRIES)
        result, _ = self._call(client)
        assert not result.ok
        assert result.error_kind == "server_error"
        assert result.status_code == 500
        assert client.post.call_count == _SERVER_ERROR_RETRIES

    def test_network_error_after_retries_is_network_error(self):
        from provisioning.registration import _CONNECT_RETRIES

        client = _client_returning(*[httpx.ConnectError("refused")] * _CONNECT_RETRIES)
        result, _ = self._call(client)
        assert result.error_kind == "network_error"
        assert result.status_code is None
        assert result.reason.startswith("Could not reach command center")

    def test_timeout_is_retried_as_network_error(self):
        client = _client_returning(
            httpx.ReadTimeout("slow"),
            _resp(201, {"node_id": "node-1", "node_key": "k"}),
        )
        result, sleep = self._call(client)
        assert result.ok
        assert sleep.call_count == 1

    def test_2xx_without_node_key_is_bad_response(self):
        client = _client_returning(_resp(201, {"node_id": "node-1"}))
        result, _ = self._call(client)
        assert not result.ok
        assert result.error_kind == "bad_response"

    def test_token_never_logged(self):
        client = _client_returning(_resp(401, {"detail": "nope"}))
        with patch("provisioning.registration.logger") as mock_logger:
            self._call(client)
        assert mock_logger.method_calls
        for call in mock_logger.method_calls:
            assert "SECRET-123" not in repr(call)
