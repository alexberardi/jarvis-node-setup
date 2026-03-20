"""
Unit tests for BluetoothCommand.

Tests command properties, schema generation, and run() for each action
with a mocked BluetoothService.
"""

import pytest
from unittest.mock import MagicMock, patch

from commands.bluetooth_command import BluetoothCommand
from core.platform_abstraction import BluetoothDevice
from core.request_information import RequestInformation
from services.bluetooth_service import BluetoothPairResult, BluetoothRole, BluetoothService


@pytest.fixture
def command():
    """Create a BluetoothCommand with mocked service."""
    cmd = BluetoothCommand()
    cmd._service = MagicMock(spec=BluetoothService)
    cmd._service.is_available.return_value = True
    return cmd


@pytest.fixture
def request_info():
    """Create a mock RequestInformation."""
    return RequestInformation(
        voice_command="bluetooth scan",
        conversation_id="test-conversation-123",
    )


@pytest.fixture
def sample_device():
    return BluetoothDevice(
        name="JBL Flip 6",
        mac_address="AA:BB:CC:DD:EE:FF",
        device_type="audio_sink",
        paired=True,
        connected=True,
    )


class TestBluetoothCommandProperties:
    def test_command_name(self, command):
        assert command.command_name == "bluetooth"

    def test_description(self, command):
        assert "bluetooth" in command.description.lower()

    def test_keywords(self, command):
        keywords = command.keywords
        assert "bluetooth" in keywords
        assert "pair" in keywords
        assert "connect" in keywords

    def test_parameters(self, command):
        params = command.parameters
        param_names = [p.name for p in params]
        assert "action" in param_names
        assert "device_name" in param_names
        assert "role" in param_names

        action_param = next(p for p in params if p.name == "action")
        assert action_param.required is True
        assert "scan" in action_param.enum_values
        assert "pair" in action_param.enum_values

    def test_required_secrets_empty(self, command):
        assert command.required_secrets == []

    def test_prompt_examples(self, command):
        examples = command.generate_prompt_examples()
        assert len(examples) >= 3
        assert any(ex.is_primary for ex in examples)

    def test_adapter_examples(self, command):
        examples = command.generate_adapter_examples()
        assert len(examples) >= 10
        actions = {ex.expected_parameters.get("action") for ex in examples}
        assert "scan" in actions
        assert "pair" in actions
        assert "connect" in actions
        assert "disconnect" in actions
        assert "status" in actions


class TestBluetoothNotAvailable:
    def test_returns_error_when_unavailable(self, command, request_info):
        command._service.is_available.return_value = False

        response = command.run(request_info, action="scan")

        assert response.success is False
        assert "not available" in response.error_details.lower()


class TestScanAction:
    def test_scan_returns_devices(self, command, request_info, sample_device):
        command._service.scan_for_devices.return_value = [sample_device]

        response = command.run(request_info, action="scan")

        assert response.success is True
        assert len(response.context_data["devices"]) == 1
        assert response.context_data["devices"][0]["name"] == "JBL Flip 6"

    def test_scan_empty(self, command, request_info):
        command._service.scan_for_devices.return_value = []

        response = command.run(request_info, action="scan")

        assert response.success is True
        assert response.context_data["devices"] == []


class TestPairAction:
    def test_pair_phone_makes_discoverable(self, command, request_info):
        command._service.make_discoverable.return_value = True

        response = command.run(request_info, action="pair", role="phone")

        assert response.success is True
        assert response.context_data["discoverable"] is True
        command._service.make_discoverable.assert_called_once_with(timeout=120)

    def test_pair_phone_failure(self, command, request_info):
        command._service.make_discoverable.return_value = False

        response = command.run(request_info, action="pair", role="phone")

        assert response.success is False

    def test_pair_speaker_triggers_scan(self, command, request_info):
        command._service.scan_for_devices.return_value = []

        response = command.run(request_info, action="pair", role="speaker")

        assert response.success is True
        command._service.scan_for_devices.assert_called_once()


class TestConnectAction:
    def test_connect_no_device_name(self, command, request_info):
        response = command.run(request_info, action="connect")

        assert response.success is False
        assert "specify" in response.error_details.lower()

    def test_connect_to_paired_device(self, command, request_info, sample_device):
        command._service.get_paired_devices.return_value = [sample_device]
        command._service.connect_device.return_value = True

        response = command.run(request_info, action="connect", device_name="JBL", role="speaker")

        assert response.success is True
        assert response.context_data["role"] == "source"

    def test_connect_scans_if_not_paired(self, command, request_info, sample_device):
        command._service.get_paired_devices.return_value = []
        command._service.scan_for_devices.return_value = [sample_device]
        command._service.pair_and_connect.return_value = BluetoothPairResult(
            success=True, device=sample_device, message="Connected to JBL Flip 6",
        )

        response = command.run(request_info, action="connect", device_name="JBL", role="speaker")

        assert response.success is True
        command._service.pair_and_connect.assert_called_once()

    def test_connect_device_not_found(self, command, request_info):
        command._service.get_paired_devices.return_value = []
        command._service.scan_for_devices.return_value = []

        response = command.run(request_info, action="connect", device_name="NonExistent")

        assert response.success is False
        assert "could not find" in response.error_details.lower()


class TestDisconnectAction:
    def test_disconnect_by_name(self, command, request_info, sample_device):
        command._service.get_connected_devices.return_value = [sample_device]

        response = command.run(request_info, action="disconnect", device_name="JBL")

        assert response.success is True
        command._service.disconnect_device.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_disconnect_all(self, command, request_info, sample_device):
        command._service.get_connected_devices.return_value = [sample_device]

        response = command.run(request_info, action="disconnect")

        assert response.success is True
        command._service.disconnect_device.assert_called_once()

    def test_disconnect_not_found(self, command, request_info):
        command._service.get_connected_devices.return_value = []

        response = command.run(request_info, action="disconnect", device_name="NonExistent")

        assert response.success is False


class TestForgetAction:
    def test_forget_by_name(self, command, request_info, sample_device):
        command._service.get_paired_devices.return_value = [sample_device]

        response = command.run(request_info, action="forget", device_name="JBL")

        assert response.success is True
        command._service.forget_device.assert_called_once_with("AA:BB:CC:DD:EE:FF")

    def test_forget_no_name(self, command, request_info):
        response = command.run(request_info, action="forget")

        assert response.success is False
        assert "specify" in response.error_details.lower()

    def test_forget_not_found(self, command, request_info):
        command._service.get_paired_devices.return_value = []

        response = command.run(request_info, action="forget", device_name="Ghost")

        assert response.success is False


class TestStatusAction:
    def test_status_with_devices(self, command, request_info):
        command._service.get_status.return_value = {
            "available": True,
            "connected": [{"name": "JBL Flip 6", "mac": "AA:BB:CC:DD:EE:FF", "type": "audio_sink"}],
            "paired": [
                {"name": "JBL Flip 6", "mac": "AA:BB:CC:DD:EE:FF", "type": "audio_sink", "connected": True},
                {"name": "iPhone", "mac": "11:22:33:44:55:66", "type": "phone", "connected": False},
            ],
        }

        response = command.run(request_info, action="status")

        assert response.success is True
        assert "JBL Flip 6" in response.context_data["message"]
        assert "iPhone" in response.context_data["message"]

    def test_status_empty(self, command, request_info):
        command._service.get_status.return_value = {
            "available": True,
            "connected": [],
            "paired": [],
        }

        response = command.run(request_info, action="status")

        assert response.success is True
        assert "no bluetooth" in response.context_data["message"].lower()


class TestDeviceNameMatching:
    def test_exact_match(self):
        devices = [
            BluetoothDevice(name="JBL Flip 6", mac_address="AA:BB:CC:DD:EE:FF"),
            BluetoothDevice(name="iPhone", mac_address="11:22:33:44:55:66"),
        ]
        result = BluetoothCommand._find_device_by_name(devices, "iPhone")
        assert result.mac_address == "11:22:33:44:55:66"

    def test_partial_match(self):
        devices = [
            BluetoothDevice(name="JBL Flip 6", mac_address="AA:BB:CC:DD:EE:FF"),
        ]
        result = BluetoothCommand._find_device_by_name(devices, "JBL")
        assert result.mac_address == "AA:BB:CC:DD:EE:FF"

    def test_case_insensitive(self):
        devices = [
            BluetoothDevice(name="JBL Flip 6", mac_address="AA:BB:CC:DD:EE:FF"),
        ]
        result = BluetoothCommand._find_device_by_name(devices, "jbl flip")
        assert result is not None

    def test_no_match(self):
        devices = [
            BluetoothDevice(name="JBL Flip 6", mac_address="AA:BB:CC:DD:EE:FF"),
        ]
        result = BluetoothCommand._find_device_by_name(devices, "Bose")
        assert result is None
