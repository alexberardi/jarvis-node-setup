"""
Unit tests for WiFi manager implementations.
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from provisioning.models import NetworkInfo
from provisioning.wifi_manager import (
    NetworkManagerWiFi,
    SimulatedWiFi,
    get_wifi_manager,
)


class TestSimulatedWiFi:
    """Test the simulated WiFi manager."""

    @pytest.fixture
    def wifi(self):
        return SimulatedWiFi()

    def test_scan_returns_networks(self, wifi):
        networks = wifi.scan_networks()
        assert len(networks) > 0
        assert all(isinstance(n, NetworkInfo) for n in networks)

    def test_scan_returns_expected_networks(self, wifi):
        networks = wifi.scan_networks()
        ssids = [n.ssid for n in networks]
        assert "HomeNetwork" in ssids

    def test_connect_to_known_network_succeeds(self, wifi):
        result = wifi.connect("HomeNetwork", "anypassword")
        assert result is True

    def test_connect_to_unknown_network_fails(self, wifi):
        result = wifi.connect("UnknownNetwork", "password")
        assert result is False

    def test_get_current_ssid_initially_none(self, wifi):
        assert wifi.get_current_ssid() is None

    def test_get_current_ssid_after_connect(self, wifi):
        wifi.connect("HomeNetwork", "password")
        assert wifi.get_current_ssid() == "HomeNetwork"

    def test_start_ap_mode_succeeds(self, wifi):
        result = wifi.start_ap_mode("jarvis-test")
        assert result is True

    def test_ap_mode_clears_connected_ssid(self, wifi):
        wifi.connect("HomeNetwork", "password")
        wifi.start_ap_mode("jarvis-test")
        assert wifi.get_current_ssid() is None

    def test_stop_ap_mode_succeeds(self, wifi):
        wifi.start_ap_mode("jarvis-test")
        result = wifi.stop_ap_mode()
        assert result is True

    def test_connect_stops_ap_mode(self, wifi):
        wifi.start_ap_mode("jarvis-test")
        wifi.connect("HomeNetwork", "password")
        assert wifi._ap_mode_active is False


class TestNetworkManagerWiFi:
    """Test the NetworkManager WiFi implementation."""

    @pytest.fixture
    def wifi(self):
        return NetworkManagerWiFi()

    def test_scan_returns_list(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="HomeNetwork:85:WPA2\nGuest:45:WPA2\n"
            )
            networks = wifi.scan_networks()
            assert isinstance(networks, list)

    def test_scan_parses_output(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="HomeNetwork:85:WPA2\nGuest:45:WPA2\n"
            )
            networks = wifi.scan_networks()
            assert len(networks) == 2
            assert networks[0].ssid == "HomeNetwork"

    def test_scan_deduplicates_ssids(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Network1:85:WPA2\nNetwork1:80:WPA2\nNetwork2:70:WPA2\n"
            )
            networks = wifi.scan_networks()
            ssids = [n.ssid for n in networks]
            assert ssids.count("Network1") == 1

    def test_scan_returns_empty_on_error(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            networks = wifi.scan_networks()
            assert networks == []

    def test_scan_returns_empty_on_timeout(self, wifi):
        import subprocess
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("nmcli", 30)
            networks = wifi.scan_networks()
            assert networks == []

    def test_connect_calls_nmcli(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            wifi.connect("TestNetwork", "password123")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert "nmcli" in args
            assert "TestNetwork" in args

    def test_connect_returns_true_on_success(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = wifi.connect("TestNetwork", "password")
            assert result is True

    def test_connect_returns_false_on_failure(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = wifi.connect("TestNetwork", "wrong")
            assert result is False

    def test_get_current_ssid_parses_output(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="yes:MyNetwork\nno:OtherNetwork\n"
            )
            ssid = wifi.get_current_ssid()
            assert ssid == "MyNetwork"

    def test_get_current_ssid_returns_none_if_not_connected(self, wifi):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="no:Network1\nno:Network2\n"
            )
            ssid = wifi.get_current_ssid()
            assert ssid is None


class TestGetWifiManager:
    """Test the factory function for getting WiFi manager."""

    def test_returns_simulated_when_env_true(self):
        with patch.dict(os.environ, {"JARVIS_SIMULATE_PROVISIONING": "true"}):
            manager = get_wifi_manager()
            assert isinstance(manager, SimulatedWiFi)

    def test_returns_simulated_when_env_1(self):
        with patch.dict(os.environ, {"JARVIS_SIMULATE_PROVISIONING": "1"}):
            manager = get_wifi_manager()
            assert isinstance(manager, SimulatedWiFi)

    def test_returns_simulated_when_env_yes(self):
        with patch.dict(os.environ, {"JARVIS_SIMULATE_PROVISIONING": "yes"}):
            manager = get_wifi_manager()
            assert isinstance(manager, SimulatedWiFi)

    def test_returns_real_when_env_false(self):
        with patch.dict(os.environ, {"JARVIS_SIMULATE_PROVISIONING": "false"}):
            manager = get_wifi_manager()
            assert isinstance(manager, NetworkManagerWiFi)

    def test_returns_real_when_env_not_set(self):
        env_copy = os.environ.copy()
        env_copy.pop("JARVIS_SIMULATE_PROVISIONING", None)
        with patch.dict(os.environ, env_copy, clear=True):
            manager = get_wifi_manager()
            assert isinstance(manager, NetworkManagerWiFi)
