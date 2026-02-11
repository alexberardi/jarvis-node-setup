"""
Unit tests for authorize_node.py token-based provisioning functions.
"""

from unittest.mock import MagicMock, patch

import httpx
import pytest


class TestCreateProvisioningToken:
    """Test create_provisioning_token function."""

    def _import(self):
        from utils.authorize_node import create_provisioning_token
        return create_provisioning_token

    def test_posts_to_provisioning_token_endpoint(self):
        """URL must be /api/v0/provisioning/token."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "provisioning_token": "tok_abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "admin_key", "hh-uuid")

            called_url = mock_client.post.call_args[0][0]
            assert called_url == "http://cc:8002/api/v0/provisioning/token"

    def test_sends_admin_key_in_header(self):
        """Must send X-API-Key header with admin key."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "provisioning_token": "tok_abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "my-admin-key", "hh-uuid")

            call_kwargs = mock_client.post.call_args[1]
            assert call_kwargs["headers"]["X-API-Key"] == "my-admin-key"

    def test_returns_node_id_and_token(self):
        """Should return dict with node_id and provisioning_token."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "provisioning_token": "tok_abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            result = fn("http://cc:8002", "admin_key", "hh-uuid")

            assert result is not None
            assert result["node_id"] == "uuid-123"
            assert result["provisioning_token"] == "tok_abc"

    def test_sends_household_id_in_payload(self):
        """Payload must contain household_id."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "provisioning_token": "tok_abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "admin_key", "hh-uuid")

            payload = mock_client.post.call_args[1]["json"]
            assert payload["household_id"] == "hh-uuid"

    def test_sends_optional_room_and_name(self):
        """Optional room and name should be included when provided."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "provisioning_token": "tok_abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "admin_key", "hh-uuid", room="kitchen", name="Kitchen Node")

            payload = mock_client.post.call_args[1]["json"]
            assert payload["room"] == "kitchen"
            assert payload["name"] == "Kitchen Node"

    def test_omits_optional_fields_when_none(self):
        """Optional fields should not be in payload when None."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "provisioning_token": "tok_abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "admin_key", "hh-uuid")

            payload = mock_client.post.call_args[1]["json"]
            assert "room" not in payload
            assert "name" not in payload

    def test_returns_none_on_failure(self):
        """Should return None on non-2xx status."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"detail": "Internal error"}

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            result = fn("http://cc:8002", "admin_key", "hh-uuid")
            assert result is None


class TestRegisterWithToken:
    """Test register_with_token function."""

    def _import(self):
        from utils.authorize_node import register_with_token
        return register_with_token

    def test_posts_to_nodes_register_endpoint(self):
        """URL must be /api/v0/nodes/register."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "node_key": "key-abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "uuid-123", "tok_abc")

            called_url = mock_client.post.call_args[0][0]
            assert called_url == "http://cc:8002/api/v0/nodes/register"

    def test_sends_node_id_and_token_in_payload(self):
        """Payload must contain node_id and provisioning_token."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "node_key": "key-abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "uuid-123", "tok_abc")

            payload = mock_client.post.call_args[1]["json"]
            assert payload["node_id"] == "uuid-123"
            assert payload["provisioning_token"] == "tok_abc"

    def test_no_admin_key_header(self):
        """Must NOT send X-API-Key header."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "node_key": "key-abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            fn("http://cc:8002", "uuid-123", "tok_abc")

            call_kwargs = mock_client.post.call_args[1]
            headers = call_kwargs.get("headers", {})
            assert "X-API-Key" not in headers

    def test_returns_node_id_and_node_key(self):
        """Should return dict with node_id and node_key."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 201
        mock_response.json.return_value = {
            "node_id": "uuid-123",
            "node_key": "key-abc",
        }

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            result = fn("http://cc:8002", "uuid-123", "tok_abc")

            assert result is not None
            assert result["node_id"] == "uuid-123"
            assert result["node_key"] == "key-abc"

    def test_returns_none_on_401(self):
        """Should return None on 401."""
        fn = self._import()
        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.return_value = mock_response
            mock_cls.return_value = mock_client

            result = fn("http://cc:8002", "uuid-123", "tok_expired")
            assert result is None

    def test_returns_none_on_network_error(self):
        """Should return None on network errors."""
        fn = self._import()

        with patch("utils.authorize_node.httpx.Client") as mock_cls:
            mock_client = MagicMock()
            mock_client.__enter__ = MagicMock(return_value=mock_client)
            mock_client.__exit__ = MagicMock(return_value=False)
            mock_client.post.side_effect = httpx.ConnectError("Connection refused")
            mock_cls.return_value = mock_client

            result = fn("http://cc:8002", "uuid-123", "tok_abc")
            assert result is None


class TestExistingAdminOperationsUnchanged:
    """Verify that list/delete/update still use admin key as before."""

    def test_list_nodes_uses_admin_key(self):
        from utils.authorize_node import list_nodes

        with patch("utils.authorize_node._make_cc_request") as mock_req:
            mock_req.return_value = (200, [])

            list_nodes("http://cc:8002", "my-admin-key")

            mock_req.assert_called_once_with(
                "GET", "http://cc:8002/api/v0/admin/nodes", "my-admin-key"
            )

    def test_delete_node_uses_admin_key(self):
        from utils.authorize_node import delete_node

        with patch("utils.authorize_node._make_cc_request") as mock_req:
            mock_req.return_value = (200, None)

            delete_node("http://cc:8002", "my-admin-key", "node-1")

            mock_req.assert_called_once_with(
                "DELETE", "http://cc:8002/api/v0/admin/nodes/node-1", "my-admin-key"
            )

    def test_update_node_uses_admin_key(self):
        from utils.authorize_node import update_node

        with patch("utils.authorize_node._make_cc_request") as mock_req:
            mock_req.return_value = (200, {"node_id": "node-1"})

            update_node("http://cc:8002", "my-admin-key", "node-1", room="bedroom")

            mock_req.assert_called_once_with(
                "PATCH",
                "http://cc:8002/api/v0/admin/nodes/node-1",
                "my-admin-key",
                {"room": "bedroom"},
            )
