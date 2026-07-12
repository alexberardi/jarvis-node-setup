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
