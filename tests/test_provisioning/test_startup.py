"""
Unit tests for startup detection logic.
"""

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from provisioning.startup import (
    clear_provisioned,
    is_provisioned,
    mark_provisioned,
)


@pytest.fixture
def temp_secret_dir(tmp_path):
    """Create a temporary secret directory for testing."""
    secret_dir = tmp_path / ".jarvis"
    secret_dir.mkdir()
    with patch("provisioning.startup.get_secret_dir", return_value=secret_dir):
        yield secret_dir


class TestMarkProvisioned:
    """Test marking a node as provisioned."""

    def test_creates_marker_file(self, temp_secret_dir):
        mark_provisioned()

        marker = temp_secret_dir / ".provisioned"
        assert marker.exists()

    def test_marker_has_restricted_permissions(self, temp_secret_dir):
        mark_provisioned()

        marker = temp_secret_dir / ".provisioned"
        # Check file permissions (600)
        mode = marker.stat().st_mode & 0o777
        assert mode == 0o600

    def test_idempotent_marking(self, temp_secret_dir):
        mark_provisioned()
        mark_provisioned()  # Should not raise

        marker = temp_secret_dir / ".provisioned"
        assert marker.exists()


class TestClearProvisioned:
    """Test clearing provisioned status."""

    def test_removes_marker_file(self, temp_secret_dir):
        mark_provisioned()
        marker = temp_secret_dir / ".provisioned"
        assert marker.exists()

        clear_provisioned()
        assert not marker.exists()

    def test_no_error_if_not_provisioned(self, temp_secret_dir):
        # Should not raise if marker doesn't exist
        clear_provisioned()


class TestIsProvisioned:
    """Test provisioning detection."""

    def test_returns_false_if_no_marker(self, temp_secret_dir):
        result = is_provisioned()
        assert result is False

    def test_returns_false_if_no_command_center_url(self, temp_secret_dir):
        mark_provisioned()

        with patch("provisioning.startup._get_command_center_url", return_value=None):
            result = is_provisioned()
            assert result is False

    def test_returns_false_if_command_center_unreachable(self, temp_secret_dir):
        mark_provisioned()

        with patch("provisioning.startup._get_command_center_url", return_value="http://localhost:7703"):
            with patch("provisioning.startup._can_reach_command_center", return_value=False):
                result = is_provisioned()
                assert result is False

    def test_returns_true_if_provisioned_and_reachable(self, temp_secret_dir):
        mark_provisioned()

        with patch("provisioning.startup._get_command_center_url", return_value="http://localhost:7703"):
            with patch("provisioning.startup._can_reach_command_center", return_value=True):
                result = is_provisioned()
                assert result is True


class TestCommandCenterUrl:
    """Test command center URL resolution."""

    def test_gets_url_from_env(self, temp_secret_dir):
        mark_provisioned()

        with patch.dict(os.environ, {"COMMAND_CENTER_URL": "http://env.example.com:7703"}):
            with patch("provisioning.startup._can_reach_command_center", return_value=True):
                result = is_provisioned()
                # If it uses the env URL and can reach it, should return True
                assert result is True

    def test_falls_back_to_config_json(self, temp_secret_dir, tmp_path):
        mark_provisioned()

        config_file = tmp_path / "config.json"
        config_file.write_text('{"jarvis_command_center_api_url": "http://config.example.com:7703"}')

        # Clear env var to force config.json fallback
        env_without_url = {k: v for k, v in os.environ.items() if k != "COMMAND_CENTER_URL"}

        with patch.dict(os.environ, {"CONFIG_PATH": str(config_file)}, clear=True):
            # Restore other env vars but remove COMMAND_CENTER_URL
            for key, value in env_without_url.items():
                os.environ[key] = value
            if "COMMAND_CENTER_URL" in os.environ:
                del os.environ["COMMAND_CENTER_URL"]

            with patch("provisioning.startup._can_reach_command_center", return_value=True):
                result = is_provisioned()
                assert result is True


class TestCanReachCommandCenter:
    """Test command center connectivity check."""

    def test_returns_true_on_200(self, temp_secret_dir):
        from provisioning.startup import _can_reach_command_center

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = mock_response
            result = _can_reach_command_center("http://localhost:7703")
            assert result is True

    def test_returns_false_on_error(self, temp_secret_dir):
        from provisioning.startup import _can_reach_command_center
        import httpx

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.side_effect = httpx.RequestError("Connection failed")
            result = _can_reach_command_center("http://localhost:7703")
            assert result is False

    def test_returns_false_on_non_200(self, temp_secret_dir):
        from provisioning.startup import _can_reach_command_center

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.Client") as mock_client:
            mock_client.return_value.__enter__.return_value.get.return_value = mock_response
            result = _can_reach_command_center("http://localhost:7703")
            assert result is False


class TestProvisioningFlow:
    """Test the full provisioning detection flow."""

    def test_fresh_node_is_not_provisioned(self, temp_secret_dir):
        """A fresh node with no marker should not be provisioned."""
        result = is_provisioned()
        assert result is False

    def test_mark_then_check(self, temp_secret_dir):
        """After marking, node should be provisioned (if command center reachable)."""
        with patch("provisioning.startup._get_command_center_url", return_value="http://localhost:7703"):
            with patch("provisioning.startup._can_reach_command_center", return_value=True):
                mark_provisioned()
                result = is_provisioned()
                assert result is True

    def test_clear_then_check(self, temp_secret_dir):
        """After clearing, node should not be provisioned."""
        with patch("provisioning.startup._get_command_center_url", return_value="http://localhost:7703"):
            with patch("provisioning.startup._can_reach_command_center", return_value=True):
                mark_provisioned()
                assert is_provisioned() is True

                clear_provisioned()
                assert is_provisioned() is False
