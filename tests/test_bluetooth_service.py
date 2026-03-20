"""
Unit tests for BluetoothService.

Tests pairing flows, audio routing, persistence (via command_data),
and auto-reconnect using a mocked BluetoothProvider.
"""

import pytest
from unittest.mock import MagicMock, patch

from core.platform_abstraction import BluetoothDevice, BluetoothProvider
from services.bluetooth_service import BluetoothRole, BluetoothService


@pytest.fixture
def mock_provider():
    """Create a mock BluetoothProvider."""
    provider = MagicMock(spec=BluetoothProvider)
    provider.is_available.return_value = True
    return provider


@pytest.fixture
def service(mock_provider):
    """Create a BluetoothService with mocked provider."""
    return BluetoothService(mock_provider)


@pytest.fixture
def sample_device():
    """A sample Bluetooth device."""
    return BluetoothDevice(
        name="JBL Flip 6",
        mac_address="AA:BB:CC:DD:EE:FF",
        device_type="audio_sink",
        paired=True,
        connected=True,
    )


class TestIsAvailable:
    def test_available(self, service, mock_provider):
        assert service.is_available() is True
        mock_provider.is_available.assert_called_once()

    def test_not_available(self, service, mock_provider):
        mock_provider.is_available.return_value = False
        assert service.is_available() is False


class TestScan:
    def test_scan_returns_devices(self, service, mock_provider, sample_device):
        mock_provider.scan.return_value = [sample_device]
        devices = service.scan_for_devices(timeout=5.0)
        assert len(devices) == 1
        assert devices[0].name == "JBL Flip 6"
        mock_provider.scan.assert_called_once_with(timeout=5.0)

    def test_scan_empty(self, service, mock_provider):
        mock_provider.scan.return_value = []
        devices = service.scan_for_devices()
        assert devices == []


class TestMakeDiscoverable:
    def test_make_discoverable(self, service, mock_provider):
        mock_provider.set_discoverable.return_value = True
        assert service.make_discoverable(timeout=60) is True
        mock_provider.set_discoverable.assert_called_once_with(enabled=True, timeout=60)


class TestPairAndConnect:
    @patch("services.bluetooth_service.SessionLocal")
    def test_successful_pair(self, mock_session_cls, service, mock_provider, sample_device):
        mock_provider.trust.return_value = True
        mock_provider.pair.return_value = True
        mock_provider.connect.return_value = True
        mock_provider.get_paired_devices.return_value = [sample_device]

        result = service.pair_and_connect("AA:BB:CC:DD:EE:FF", BluetoothRole.SOURCE)

        assert result.success is True
        assert result.device.name == "JBL Flip 6"
        mock_provider.trust.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        mock_provider.pair.assert_called_once_with("AA:BB:CC:DD:EE:FF")
        mock_provider.connect.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_trust_fails(self, service, mock_provider):
        mock_provider.trust.return_value = False

        result = service.pair_and_connect("AA:BB:CC:DD:EE:FF")

        assert result.success is False
        assert "trust" in result.message.lower()
        mock_provider.pair.assert_not_called()

    def test_pair_fails(self, service, mock_provider):
        mock_provider.trust.return_value = True
        mock_provider.pair.return_value = False

        result = service.pair_and_connect("AA:BB:CC:DD:EE:FF")

        assert result.success is False
        assert "pair" in result.message.lower()
        mock_provider.connect.assert_not_called()

    def test_connect_fails(self, service, mock_provider):
        mock_provider.trust.return_value = True
        mock_provider.pair.return_value = True
        mock_provider.connect.return_value = False

        result = service.pair_and_connect("AA:BB:CC:DD:EE:FF")

        assert result.success is False
        assert "connect" in result.message.lower()


class TestDisconnect:
    def test_disconnect(self, service, mock_provider):
        mock_provider.disconnect.return_value = True
        assert service.disconnect_device("AA:BB:CC:DD:EE:FF") is True
        mock_provider.disconnect.assert_called_once_with("AA:BB:CC:DD:EE:FF")


class TestForget:
    @patch("services.bluetooth_service.SessionLocal")
    def test_forget(self, mock_session_cls, service, mock_provider):
        assert service.forget_device("AA:BB:CC:DD:EE:FF") is True
        mock_provider.remove.assert_called_once_with("AA:BB:CC:DD:EE:FF")


class TestConfigureAudioRoute:
    @patch("services.bluetooth_service.subprocess")
    def test_source_sets_default_sink(self, mock_subprocess, service):
        mock_subprocess.run.return_value = MagicMock(returncode=0)

        result = service.configure_audio_route("AA:BB:CC:DD:EE:FF", BluetoothRole.SOURCE)

        assert result is True
        mock_subprocess.run.assert_called_once()
        args = mock_subprocess.run.call_args[0][0]
        assert args[0] == "pactl"
        assert args[1] == "set-default-sink"
        assert "AA_BB_CC_DD_EE_FF" in args[2]

    @patch("services.bluetooth_service.subprocess")
    def test_bridge_loads_loopback(self, mock_subprocess, service):
        mock_subprocess.run.return_value = MagicMock(returncode=0)

        result = service.configure_audio_route("AA:BB:CC:DD:EE:FF", BluetoothRole.BRIDGE)

        assert result is True
        args = mock_subprocess.run.call_args[0][0]
        assert "module-loopback" in args

    @patch("services.bluetooth_service.subprocess")
    def test_sink_auto_routes(self, mock_subprocess, service):
        result = service.configure_audio_route("AA:BB:CC:DD:EE:FF", BluetoothRole.SINK)
        assert result is True
        mock_subprocess.run.assert_not_called()

    @patch("services.bluetooth_service.subprocess")
    def test_source_pactl_failure(self, mock_subprocess, service):
        mock_subprocess.run.return_value = MagicMock(returncode=1, stderr="Failure")

        result = service.configure_audio_route("AA:BB:CC:DD:EE:FF", BluetoothRole.SOURCE)

        assert result is False


class TestGetStatus:
    def test_status(self, service, mock_provider, sample_device):
        mock_provider.get_connected_devices.return_value = [sample_device]
        mock_provider.get_paired_devices.return_value = [sample_device]

        status = service.get_status()

        assert status["available"] is True
        assert len(status["connected"]) == 1
        assert status["connected"][0]["name"] == "JBL Flip 6"
        assert len(status["paired"]) == 1


class TestReconnect:
    @patch("services.bluetooth_service.SessionLocal")
    def test_reconnect_known_devices(self, mock_session_cls, service, mock_provider):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        # Mock the repo to return device records
        with patch("services.bluetooth_service.CommandDataRepository") as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_all.return_value = [
                {
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "name": "JBL Flip 6",
                    "role": "source",
                    "auto_connect": True,
                },
            ]
            mock_repo_cls.return_value = mock_repo
            mock_provider.connect.return_value = True

            count = service.reconnect_known_devices()

        assert count == 1
        mock_provider.connect.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    @patch("services.bluetooth_service.SessionLocal")
    def test_reconnect_skips_failed(self, mock_session_cls, service, mock_provider):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        with patch("services.bluetooth_service.CommandDataRepository") as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_all.return_value = [
                {
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "name": "JBL Flip 6",
                    "role": "source",
                    "auto_connect": True,
                },
            ]
            mock_repo_cls.return_value = mock_repo
            mock_provider.connect.return_value = False

            count = service.reconnect_known_devices()

        assert count == 0

    @patch("services.bluetooth_service.SessionLocal")
    def test_reconnect_skips_auto_connect_false(self, mock_session_cls, service, mock_provider):
        mock_session = MagicMock()
        mock_session_cls.return_value = mock_session

        with patch("services.bluetooth_service.CommandDataRepository") as mock_repo_cls:
            mock_repo = MagicMock()
            mock_repo.get_all.return_value = [
                {
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "name": "JBL Flip 6",
                    "role": "source",
                    "auto_connect": False,
                },
            ]
            mock_repo_cls.return_value = mock_repo

            count = service.reconnect_known_devices()

        assert count == 0
        mock_provider.connect.assert_not_called()
