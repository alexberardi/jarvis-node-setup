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
    should_enter_provisioning,
    wait_for_command_center,
    wait_for_wifi,
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
    """is_provisioned() is MARKER-ONLY.

    REGRESSION GUARD (2026-07-05 prod-kitchen stranding): the old
    implementation also required command-center reachability, so 85s of
    CC unreachability at boot (WiFi slow to associate after a watchdog
    reboot) was treated as "not provisioned" and the node entered AP
    mode — which stops NetworkManager, and whose captive DNS makes the
    recovery watcher's probe point at the node itself. Result: a
    provisioned node stranded off-network until a physical power-cycle.
    Reachability must play NO role in provisioning state.
    """

    def test_returns_false_if_no_marker(self, temp_secret_dir):
        result = is_provisioned()
        assert result is False

    def test_returns_true_with_marker_even_when_cc_unreachable(self, temp_secret_dir):
        mark_provisioned()

        with patch(
            "provisioning.startup._can_reach_command_center",
            side_effect=AssertionError("reachability must not be consulted"),
        ):
            assert is_provisioned() is True

    def test_returns_true_with_marker_and_no_cc_url(self, temp_secret_dir):
        mark_provisioned()

        with patch("provisioning.startup._get_command_center_url", return_value=None):
            assert is_provisioned() is True


class TestShouldEnterProvisioning:
    """The AP-mode decision: marker presence and NOTHING else.

    Entering AP mode tears down the WiFi client, so a wrong "yes" is
    unrecoverable without physical access. Re-provisioning a relocated
    node is an explicit user action (factory reset), never automatic.
    """

    def test_fresh_node_enters_provisioning(self, temp_secret_dir):
        assert should_enter_provisioning() is True

    def test_provisioned_node_never_enters_provisioning(self, temp_secret_dir):
        mark_provisioned()
        assert should_enter_provisioning() is False

    def test_cc_unreachability_is_irrelevant(self, temp_secret_dir):
        mark_provisioned()
        with patch(
            "provisioning.startup._can_reach_command_center",
            side_effect=AssertionError("reachability must not be consulted"),
        ):
            assert should_enter_provisioning() is False

    def test_factory_reset_re_enables_provisioning(self, temp_secret_dir):
        mark_provisioned()
        clear_provisioned()
        assert should_enter_provisioning() is True


class TestWaitForWifi:
    """WiFi-join grace at boot.

    Failure here routes a provisioned node into the RECOVERABLE AP mode
    (AP↔STA cycle) — never the old dead-end AP mode.
    """

    def test_true_immediately_when_lan_up(self, temp_secret_dir):
        with patch("provisioning.startup._has_lan_connectivity", return_value=True):
            with patch("time.sleep") as mock_sleep:
                assert wait_for_wifi() is True
                mock_sleep.assert_not_called()

    def test_false_after_grace_when_lan_never_joins(self, temp_secret_dir):
        with patch("provisioning.startup._has_lan_connectivity", return_value=False):
            with patch("time.sleep"):
                assert wait_for_wifi() is False

    def test_true_when_lan_joins_late(self, temp_secret_dir):
        # Joins on the 5th poll — a slow association must not be fatal.
        results = [False] * 4 + [True]
        with patch("provisioning.startup._has_lan_connectivity", side_effect=results):
            with patch("time.sleep"):
                assert wait_for_wifi() is True


class TestWaitForCommandCenter:
    """Boot-ordering grace wait — informational, never a provisioning signal."""

    def test_true_immediately_when_reachable(self, temp_secret_dir):
        with patch("provisioning.startup._get_command_center_url", return_value="http://localhost:7703"):
            with patch("provisioning.startup._can_reach_command_center", return_value=True):
                with patch("time.sleep") as mock_sleep:
                    assert wait_for_command_center() is True
                    mock_sleep.assert_not_called()

    def test_false_after_retries_when_unreachable(self, temp_secret_dir):
        with patch("provisioning.startup._get_command_center_url", return_value="http://localhost:7703"):
            with patch("provisioning.startup._can_reach_command_center", return_value=False):
                with patch("time.sleep"):
                    assert wait_for_command_center() is False

    def test_false_fast_when_no_url_configured(self, temp_secret_dir):
        with patch("provisioning.startup._get_command_center_url", return_value=None):
            with patch("time.sleep") as mock_sleep:
                assert wait_for_command_center() is False
                mock_sleep.assert_not_called()


class TestCommandCenterUrl:
    """Test command center URL resolution (via the grace wait)."""

    def test_gets_url_from_env(self, temp_secret_dir):
        with patch.dict(os.environ, {"COMMAND_CENTER_URL": "http://env.example.com:7703"}):
            with patch("provisioning.startup._can_reach_command_center", return_value=True):
                assert wait_for_command_center() is True

    def test_falls_back_to_config_json(self, temp_secret_dir, tmp_path):
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
                assert wait_for_command_center() is True


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
        """After marking, node is provisioned — no network required."""
        mark_provisioned()
        assert is_provisioned() is True

    def test_clear_then_check(self, temp_secret_dir):
        """After clearing, node should not be provisioned."""
        mark_provisioned()
        assert is_provisioned() is True

        clear_provisioned()
        assert is_provisioned() is False
