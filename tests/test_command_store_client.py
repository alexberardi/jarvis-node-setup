"""Tests for the command store HTTP client."""

import json
from unittest.mock import patch, MagicMock

from clients.command_store_client import CommandStoreClient


class TestCommandStoreClient:
    def _mock_response(self, data: dict, status_code: int = 200):
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = data
        resp.raise_for_status = MagicMock()
        if status_code >= 400:
            resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
        return resp

    def test_search(self):
        client = CommandStoreClient(store_url="http://localhost:7720")
        expected = {"commands": [{"command_name": "test"}], "total": 1, "page": 1, "per_page": 20}

        with patch.object(client._client, "get", return_value=self._mock_response(expected)):
            result = client.search(query="test")

        assert result["total"] == 1
        assert result["commands"][0]["command_name"] == "test"
        client.close()

    def test_get_command(self):
        client = CommandStoreClient(store_url="http://localhost:7720")
        expected = {"command_name": "test", "verified": True}

        with patch.object(client._client, "get", return_value=self._mock_response(expected)):
            result = client.get_command("test")

        assert result["verified"] is True
        client.close()

    def test_get_download_info_with_auth(self):
        client = CommandStoreClient(
            store_url="http://localhost:7720",
            jwt_token="test-jwt",
            household_id="hh-1",
        )
        expected = {"github_repo_url": "https://github.com/test/repo", "git_tag": "v1.0.0"}

        with patch.object(client._client, "get", return_value=self._mock_response(expected)) as mock_get:
            result = client.get_download_info("test", version="1.0.0")

        # Verify auth headers were sent
        call_kwargs = mock_get.call_args
        headers = call_kwargs.kwargs.get("headers", {}) if call_kwargs.kwargs else {}
        assert headers.get("Authorization") == "Bearer test-jwt"
        assert headers.get("X-Household-Id") == "hh-1"
        assert result["git_tag"] == "v1.0.0"
        client.close()

    def test_report_install_fire_and_forget(self):
        client = CommandStoreClient(store_url="http://localhost:7720")

        with patch.object(client._client, "post", side_effect=Exception("network error")):
            # Should not raise
            client.report_install("test")

        client.close()

    def test_get_categories(self):
        client = CommandStoreClient(store_url="http://localhost:7720")
        expected = {"categories": [{"name": "finance", "count": 5}]}

        with patch.object(client._client, "get", return_value=self._mock_response(expected)):
            result = client.get_categories()

        assert len(result) == 1
        assert result[0]["name"] == "finance"
        client.close()

    def test_context_manager(self):
        with CommandStoreClient(store_url="http://test") as client:
            assert client.store_url == "http://test"

    def test_auth_headers_none(self):
        client = CommandStoreClient(store_url="http://test")
        headers = client._auth_headers()
        assert headers == {}
        client.close()

    def test_url_trailing_slash_stripped(self):
        client = CommandStoreClient(store_url="http://test/")
        assert client.store_url == "http://test"
        client.close()
