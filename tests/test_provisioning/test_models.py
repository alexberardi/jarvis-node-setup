"""
Unit tests for provisioning models.
"""

import pytest
from pydantic import ValidationError

from provisioning.models import (
    NetworkInfo,
    NodeInfo,
    ProvisioningState,
    ProvisionRequest,
    ProvisionResponse,
    ProvisionStatus,
    ScanNetworksResponse,
)


class TestProvisioningState:
    """Test the ProvisioningState enum."""

    def test_all_states_exist(self):
        assert ProvisioningState.AP_MODE == "AP_MODE"
        assert ProvisioningState.CONNECTING == "CONNECTING"
        assert ProvisioningState.REGISTERING == "REGISTERING"
        assert ProvisioningState.PROVISIONED == "PROVISIONED"
        assert ProvisioningState.ERROR == "ERROR"

    def test_state_is_string(self):
        assert isinstance(ProvisioningState.AP_MODE.value, str)


class TestNetworkInfo:
    """Test the NetworkInfo model."""

    def test_valid_network(self):
        network = NetworkInfo(
            ssid="HomeNetwork",
            signal_strength=-45,
            security="WPA2"
        )
        assert network.ssid == "HomeNetwork"
        assert network.signal_strength == -45
        assert network.security == "WPA2"

    def test_missing_fields_raises_error(self):
        with pytest.raises(ValidationError):
            NetworkInfo(ssid="Test")


class TestNodeInfo:
    """Test the NodeInfo model."""

    def test_valid_node_info(self):
        info = NodeInfo(
            node_id="jarvis-a1b2c3d4",
            firmware_version="1.0.0",
            hardware="pi-zero-w",
            mac_address="b8:27:eb:a1:b2:c3",
            capabilities=["voice", "speaker"],
            state=ProvisioningState.AP_MODE
        )
        assert info.node_id == "jarvis-a1b2c3d4"
        assert info.firmware_version == "1.0.0"
        assert "voice" in info.capabilities
        assert info.state == ProvisioningState.AP_MODE

    def test_default_capabilities(self):
        info = NodeInfo(
            node_id="test",
            firmware_version="1.0.0",
            hardware="test",
            mac_address="00:00:00:00:00:00",
            state=ProvisioningState.AP_MODE
        )
        assert info.capabilities == []


class TestProvisionRequest:
    """Test the ProvisionRequest model."""

    def test_valid_request(self):
        request = ProvisionRequest(
            wifi_ssid="HomeNetwork",
            wifi_password="secret123",
            room="kitchen",
            command_center_url="http://192.168.1.50:7703",
            household_id="test-household-uuid",
            node_id="550e8400-e29b-41d4-a716-446655440000",
            provisioning_token="tok_abc123",
        )
        assert request.wifi_ssid == "HomeNetwork"
        assert request.wifi_password == "secret123"
        assert request.room == "kitchen"
        assert request.command_center_url == "http://192.168.1.50:7703"
        assert request.household_id == "test-household-uuid"
        assert request.node_id == "550e8400-e29b-41d4-a716-446655440000"
        assert request.provisioning_token == "tok_abc123"

    def test_missing_node_id_raises_error(self):
        with pytest.raises(ValidationError):
            ProvisionRequest(
                wifi_ssid="HomeNetwork",
                wifi_password="secret123",
                room="kitchen",
                command_center_url="http://192.168.1.50:7703",
                household_id="test-household-uuid",
                provisioning_token="tok_abc123",
            )

    def test_missing_provisioning_token_raises_error(self):
        with pytest.raises(ValidationError):
            ProvisionRequest(
                wifi_ssid="HomeNetwork",
                wifi_password="secret123",
                room="kitchen",
                command_center_url="http://192.168.1.50:7703",
                household_id="test-household-uuid",
                node_id="550e8400-e29b-41d4-a716-446655440000",
            )

    def test_admin_key_field_does_not_exist(self):
        """admin_key must not exist on ProvisionRequest."""
        assert "admin_key" not in ProvisionRequest.model_fields

    def test_missing_field_raises_error(self):
        with pytest.raises(ValidationError):
            ProvisionRequest(
                wifi_ssid="Test",
                wifi_password="test"
                # missing room, command_center_url, node_id, provisioning_token
            )


class TestProvisionResponse:
    """Test the ProvisionResponse model."""

    def test_success_response(self):
        response = ProvisionResponse(
            success=True,
            message="Credentials received"
        )
        assert response.success is True
        assert response.message == "Credentials received"

    def test_failure_response(self):
        response = ProvisionResponse(
            success=False,
            message="Invalid credentials"
        )
        assert response.success is False


class TestProvisionStatus:
    """Test the ProvisionStatus model."""

    def test_status_with_progress(self):
        status = ProvisionStatus(
            state=ProvisioningState.CONNECTING,
            message="Connecting to network...",
            progress_percent=50
        )
        assert status.state == ProvisioningState.CONNECTING
        assert status.progress_percent == 50
        assert status.error is None

    def test_error_status(self):
        status = ProvisionStatus(
            state=ProvisioningState.ERROR,
            message="Provisioning failed",
            progress_percent=30,
            error="Connection timeout"
        )
        assert status.state == ProvisioningState.ERROR
        assert status.error == "Connection timeout"

    def test_progress_bounds(self):
        # Test lower bound
        with pytest.raises(ValidationError):
            ProvisionStatus(
                state=ProvisioningState.AP_MODE,
                message="test",
                progress_percent=-1
            )

        # Test upper bound
        with pytest.raises(ValidationError):
            ProvisionStatus(
                state=ProvisioningState.AP_MODE,
                message="test",
                progress_percent=101
            )


class TestScanNetworksResponse:
    """Test the ScanNetworksResponse model."""

    def test_empty_networks(self):
        response = ScanNetworksResponse()
        assert response.networks == []

    def test_with_networks(self):
        networks = [
            NetworkInfo(ssid="Net1", signal_strength=-40, security="WPA2"),
            NetworkInfo(ssid="Net2", signal_strength=-70, security="OPEN"),
        ]
        response = ScanNetworksResponse(networks=networks)
        assert len(response.networks) == 2
        assert response.networks[0].ssid == "Net1"
