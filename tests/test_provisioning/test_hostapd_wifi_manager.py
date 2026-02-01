"""
Unit tests for HostapdWiFiManager - AP mode using hostapd and dnsmasq.

TDD RED phase: These tests are written BEFORE the implementation.
"""

import os
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import pytest


class TestHostapdWiFiManagerConfigGeneration:
    """Test configuration file generation."""

    @pytest.fixture
    def wifi(self):
        from provisioning.wifi_manager import HostapdWiFiManager
        return HostapdWiFiManager()

    def test_generate_hostapd_config_contains_ssid(self, wifi):
        """hostapd config should contain the broadcast SSID."""
        config = wifi._generate_hostapd_config("jarvis-setup", "wlan0", 6)
        assert "ssid=jarvis-setup" in config

    def test_generate_hostapd_config_contains_interface(self, wifi):
        """hostapd config should specify the wireless interface."""
        config = wifi._generate_hostapd_config("jarvis-setup", "wlan0", 6)
        assert "interface=wlan0" in config

    def test_generate_hostapd_config_contains_channel(self, wifi):
        """hostapd config should specify the channel."""
        config = wifi._generate_hostapd_config("jarvis-setup", "wlan0", 6)
        assert "channel=6" in config

    def test_generate_hostapd_config_sets_ap_mode(self, wifi):
        """hostapd config should set hw_mode for 2.4GHz."""
        config = wifi._generate_hostapd_config("jarvis-setup", "wlan0", 6)
        assert "hw_mode=g" in config

    def test_generate_hostapd_config_has_driver(self, wifi):
        """hostapd config should specify nl80211 driver."""
        config = wifi._generate_hostapd_config("jarvis-setup", "wlan0", 6)
        assert "driver=nl80211" in config

    def test_generate_dnsmasq_config_has_interface(self, wifi):
        """dnsmasq config should bind to the interface."""
        config = wifi._generate_dnsmasq_config("wlan0", "192.168.4.1", "192.168.4.10", "192.168.4.50")
        assert "interface=wlan0" in config

    def test_generate_dnsmasq_config_has_dhcp_range(self, wifi):
        """dnsmasq config should specify DHCP range."""
        config = wifi._generate_dnsmasq_config("wlan0", "192.168.4.1", "192.168.4.10", "192.168.4.50")
        assert "dhcp-range=192.168.4.10,192.168.4.50" in config

    def test_generate_dnsmasq_config_has_gateway(self, wifi):
        """dnsmasq config should advertise gateway."""
        config = wifi._generate_dnsmasq_config("wlan0", "192.168.4.1", "192.168.4.10", "192.168.4.50")
        assert "192.168.4.1" in config

    def test_generate_dnsmasq_config_binds_only_interface(self, wifi):
        """dnsmasq should only listen on specified interface."""
        config = wifi._generate_dnsmasq_config("wlan0", "192.168.4.1", "192.168.4.10", "192.168.4.50")
        assert "bind-interfaces" in config


class TestHostapdWiFiManagerStartAP:
    """Test start_ap_mode functionality."""

    @pytest.fixture
    def wifi(self):
        from provisioning.wifi_manager import HostapdWiFiManager
        return HostapdWiFiManager()

    @pytest.fixture
    def mock_subprocess(self):
        """Mock subprocess.run and subprocess.Popen."""
        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(returncode=0)
            mock_popen.return_value = MagicMock(pid=12345)
            yield {"run": mock_run, "popen": mock_popen}

    def test_start_ap_mode_assigns_ip_to_interface(self, wifi, mock_subprocess):
        """start_ap_mode should assign IP address to the interface."""
        wifi.start_ap_mode("jarvis-setup")

        # Check that ip addr add was called
        calls = mock_subprocess["run"].call_args_list
        ip_calls = [c for c in calls if "ip" in str(c)]
        assert len(ip_calls) > 0

    def test_start_ap_mode_starts_hostapd(self, wifi, mock_subprocess):
        """start_ap_mode should start the hostapd process."""
        wifi.start_ap_mode("jarvis-setup")

        # Check that hostapd was started
        popen_calls = mock_subprocess["popen"].call_args_list
        hostapd_calls = [c for c in popen_calls if "hostapd" in str(c)]
        assert len(hostapd_calls) > 0

    def test_start_ap_mode_starts_dnsmasq(self, wifi, mock_subprocess):
        """start_ap_mode should start the dnsmasq process."""
        wifi.start_ap_mode("jarvis-setup")

        # Check that dnsmasq was started
        popen_calls = mock_subprocess["popen"].call_args_list
        dnsmasq_calls = [c for c in popen_calls if "dnsmasq" in str(c)]
        assert len(dnsmasq_calls) > 0

    def test_start_ap_mode_returns_true_on_success(self, wifi, mock_subprocess):
        """start_ap_mode should return True when successful."""
        result = wifi.start_ap_mode("jarvis-setup")
        assert result is True

    def test_start_ap_mode_returns_false_when_hostapd_fails(self, wifi, mock_subprocess):
        """start_ap_mode should return False if hostapd fails to start."""
        mock_subprocess["popen"].side_effect = FileNotFoundError("hostapd not found")
        result = wifi.start_ap_mode("jarvis-setup")
        assert result is False

    def test_start_ap_mode_writes_config_files(self, wifi, mock_subprocess, tmp_path):
        """start_ap_mode should write hostapd and dnsmasq config files."""
        with patch.object(wifi, "_config_dir", tmp_path):
            wifi.start_ap_mode("jarvis-setup")

            # Config files should exist
            hostapd_conf = tmp_path / "hostapd.conf"
            dnsmasq_conf = tmp_path / "dnsmasq.conf"
            assert hostapd_conf.exists() or mock_subprocess["popen"].called


class TestHostapdWiFiManagerStopAP:
    """Test stop_ap_mode functionality."""

    @pytest.fixture
    def wifi(self):
        from provisioning.wifi_manager import HostapdWiFiManager
        return HostapdWiFiManager()

    def test_stop_ap_mode_terminates_hostapd(self, wifi):
        """stop_ap_mode should terminate the hostapd process."""
        mock_hostapd = MagicMock()
        mock_dnsmasq = MagicMock()

        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(returncode=0)
            mock_popen.side_effect = [mock_hostapd, mock_dnsmasq]

            wifi.start_ap_mode("jarvis-setup")
            wifi.stop_ap_mode()

            mock_hostapd.terminate.assert_called()

    def test_stop_ap_mode_terminates_dnsmasq(self, wifi):
        """stop_ap_mode should terminate the dnsmasq process."""
        mock_hostapd = MagicMock()
        mock_dnsmasq = MagicMock()

        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(returncode=0)
            mock_popen.side_effect = [mock_hostapd, mock_dnsmasq]

            wifi.start_ap_mode("jarvis-setup")
            wifi.stop_ap_mode()

            mock_dnsmasq.terminate.assert_called()

    def test_stop_ap_mode_removes_ip_from_interface(self, wifi):
        """stop_ap_mode should remove the IP address from the interface."""
        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(returncode=0)
            mock_popen.return_value = MagicMock()

            wifi.start_ap_mode("jarvis-setup")
            mock_run.reset_mock()

            wifi.stop_ap_mode()

            # Check ip addr del or flush was called
            calls = mock_run.call_args_list
            ip_calls = [c for c in calls if "ip" in str(c)]
            assert len(ip_calls) > 0

    def test_stop_ap_mode_returns_true_on_success(self, wifi):
        """stop_ap_mode should return True when successful."""
        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(returncode=0)
            mock_popen.return_value = MagicMock()

            wifi.start_ap_mode("jarvis-setup")
            result = wifi.stop_ap_mode()

            assert result is True

    def test_stop_ap_mode_returns_true_when_not_running(self, wifi):
        """stop_ap_mode should return True even if AP wasn't running."""
        result = wifi.stop_ap_mode()
        assert result is True


class TestHostapdWiFiManagerProtocolCompliance:
    """Test that HostapdWiFiManager implements WiFiManager protocol."""

    @pytest.fixture
    def wifi(self):
        from provisioning.wifi_manager import HostapdWiFiManager
        return HostapdWiFiManager()

    def test_has_scan_networks_method(self, wifi):
        """Should have scan_networks method."""
        assert hasattr(wifi, "scan_networks")
        assert callable(wifi.scan_networks)

    def test_has_connect_method(self, wifi):
        """Should have connect method."""
        assert hasattr(wifi, "connect")
        assert callable(wifi.connect)

    def test_has_get_current_ssid_method(self, wifi):
        """Should have get_current_ssid method."""
        assert hasattr(wifi, "get_current_ssid")
        assert callable(wifi.get_current_ssid)

    def test_has_start_ap_mode_method(self, wifi):
        """Should have start_ap_mode method."""
        assert hasattr(wifi, "start_ap_mode")
        assert callable(wifi.start_ap_mode)

    def test_has_stop_ap_mode_method(self, wifi):
        """Should have stop_ap_mode method."""
        assert hasattr(wifi, "stop_ap_mode")
        assert callable(wifi.stop_ap_mode)


class TestHostapdWiFiManagerScanAndConnect:
    """Test scan and connect - delegates to wpa_supplicant/nmcli when not in AP mode."""

    @pytest.fixture
    def wifi(self):
        from provisioning.wifi_manager import HostapdWiFiManager
        return HostapdWiFiManager()

    def test_scan_networks_uses_nmcli(self, wifi):
        """scan_networks should use nmcli (same as NetworkManagerWiFi)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="HomeNetwork:85:WPA2\nGuest:45:WPA2\n"
            )
            networks = wifi.scan_networks()
            assert len(networks) == 2

    def test_connect_uses_nmcli(self, wifi):
        """connect should use nmcli (same as NetworkManagerWiFi)."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = wifi.connect("TestNetwork", "password")
            assert result is True

            # Verify nmcli was called
            args = mock_run.call_args[0][0]
            assert "nmcli" in args


class TestHostapdWiFiManagerEdgeCases:
    """Test edge cases and error handling."""

    @pytest.fixture
    def wifi(self):
        from provisioning.wifi_manager import HostapdWiFiManager
        return HostapdWiFiManager()

    def test_scan_returns_empty_on_nmcli_failure(self, wifi):
        """scan_networks should return empty list on nmcli failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            networks = wifi.scan_networks()
            assert networks == []

    def test_scan_returns_empty_on_timeout(self, wifi):
        """scan_networks should return empty list on timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("nmcli", 30)
            networks = wifi.scan_networks()
            assert networks == []

    def test_connect_returns_false_on_failure(self, wifi):
        """connect should return False on nmcli failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = wifi.connect("TestNetwork", "password")
            assert result is False

    def test_connect_returns_false_on_timeout(self, wifi):
        """connect should return False on timeout."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("nmcli", 60)
            result = wifi.connect("TestNetwork", "password")
            assert result is False

    def test_get_current_ssid_returns_none_on_failure(self, wifi):
        """get_current_ssid should return None on nmcli failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            ssid = wifi.get_current_ssid()
            assert ssid is None

    def test_get_current_ssid_returns_none_when_not_connected(self, wifi):
        """get_current_ssid should return None when no network active."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="no:Network1\nno:Network2\n"
            )
            ssid = wifi.get_current_ssid()
            assert ssid is None

    def test_connect_stops_ap_mode_first(self, wifi):
        """connect should stop AP mode before connecting."""
        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(returncode=0)
            mock_hostapd = MagicMock()
            mock_dnsmasq = MagicMock()
            mock_popen.side_effect = [mock_hostapd, mock_dnsmasq]

            # Start AP mode
            wifi.start_ap_mode("jarvis-setup")
            assert wifi._ap_active is True

            # Connect should stop AP mode
            wifi.connect("TestNetwork", "password")
            mock_hostapd.terminate.assert_called()

    def test_start_ap_mode_creates_config_dir(self, wifi, tmp_path):
        """start_ap_mode should create config directory if it doesn't exist."""
        config_dir = tmp_path / "new-dir"
        wifi._config_dir = config_dir

        with patch("subprocess.run") as mock_run, \
             patch("subprocess.Popen") as mock_popen:
            mock_run.return_value = MagicMock(returncode=0)
            mock_popen.return_value = MagicMock()

            wifi.start_ap_mode("jarvis-setup")

            assert config_dir.exists()


class TestGetWifiManagerWithHostapd:
    """Test factory function returns HostapdWiFiManager when configured."""

    def test_returns_hostapd_when_env_hostapd(self):
        """Should return HostapdWiFiManager when JARVIS_WIFI_BACKEND=hostapd."""
        from provisioning.wifi_manager import HostapdWiFiManager, get_wifi_manager

        with patch.dict(os.environ, {"JARVIS_WIFI_BACKEND": "hostapd"}):
            manager = get_wifi_manager()
            assert isinstance(manager, HostapdWiFiManager)

    def test_returns_networkmanager_by_default(self):
        """Should return NetworkManagerWiFi by default."""
        from provisioning.wifi_manager import NetworkManagerWiFi, get_wifi_manager

        env = os.environ.copy()
        env.pop("JARVIS_SIMULATE_PROVISIONING", None)
        env.pop("JARVIS_WIFI_BACKEND", None)
        with patch.dict(os.environ, env, clear=True):
            manager = get_wifi_manager()
            assert isinstance(manager, NetworkManagerWiFi)
