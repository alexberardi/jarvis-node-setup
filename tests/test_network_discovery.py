#!/usr/bin/env python3
"""
Test script for network discovery functionality
"""

import sys
import os
import json
import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from utils.network_discovery_service import get_network_discovery_service, DiscoveredDevice


class TestNetworkDiscovery:
    """Test the network discovery service functionality"""
    
    def setup_method(self):
        """Set up the discovery service for testing"""
        self.discovery_service = get_network_discovery_service()
    
    def test_discovery_service_initialization(self):
        """Test that the discovery service can be initialized"""
        assert self.discovery_service is not None
        assert hasattr(self.discovery_service, 'scan_network')
        assert hasattr(self.discovery_service, 'get_discovery_summary')
        assert hasattr(self.discovery_service, 'get_jarvis_nodes')
        assert hasattr(self.discovery_service, 'get_smart_devices')
    
    def test_discovered_device_structure(self):
        """Test that DiscoveredDevice has expected structure"""
        device = DiscoveredDevice(
            ip_address="192.168.1.100",
            hostname="test-device",
            device_type="smart_plug",
            manufacturer="Test Manufacturer",
            model="Test Model",
            open_ports=[80, 443],
            services=["http", "https"],
            mac_address="00:11:22:33:44:55",
            is_jarvis_node=False,
            confidence_score=0.8
        )
        
        assert device.ip_address == "192.168.1.100"
        assert device.hostname == "test-device"
        assert device.device_type == "smart_plug"
        assert device.manufacturer == "Test Manufacturer"
        assert device.model == "Test Model"
        assert device.open_ports == [80, 443]
        assert device.services == ["http", "https"]
        assert device.mac_address == "00:11:22:33:44:55"
        assert device.is_jarvis_node is False
        assert device.confidence_score == 0.8
    
    def test_discovered_device_to_dict(self):
        """Test that DiscoveredDevice can be converted to dictionary"""
        device = DiscoveredDevice(
            ip_address="192.168.1.100",
            hostname="test-device",
            device_type="smart_plug"
        )
        
        device_dict = device.to_dict()
        assert isinstance(device_dict, dict)
        assert device_dict["ip_address"] == "192.168.1.100"
        assert device_dict["hostname"] == "test-device"
        assert device_dict["device_type"] == "smart_plug"
        assert "open_ports" in device_dict
        assert "services" in device_dict
    
    def test_discovery_summary_structure(self):
        """Test that discovery summary has expected structure"""
        summary = self.discovery_service.get_discovery_summary()
        assert isinstance(summary, dict)
        assert 'total_devices' in summary
        assert 'smart_devices' in summary
        assert 'jarvis_nodes' in summary
        assert 'devices' in summary
    
    def test_jarvis_nodes_detection(self):
        """Test that Jarvis nodes can be detected"""
        jarvis_nodes = self.discovery_service.get_jarvis_nodes()
        assert isinstance(jarvis_nodes, list)
        # Jarvis nodes should have required attributes
        for node in jarvis_nodes:
            assert hasattr(node, 'ip_address')
            assert hasattr(node, 'services')
    
    def test_smart_devices_detection(self):
        """Test that smart devices can be detected"""
        smart_devices = self.discovery_service.get_smart_devices()
        assert isinstance(smart_devices, list)
        # Smart devices should have required attributes
        for device in smart_devices:
            assert hasattr(device, 'ip_address')
            assert hasattr(device, 'device_type')
    
    def test_common_ports_structure(self):
        """Test that common ports are properly defined"""
        assert hasattr(self.discovery_service, 'common_ports')
        assert isinstance(self.discovery_service.common_ports, dict)
        # Check for some expected ports
        assert 80 in self.discovery_service.common_ports
        assert 443 in self.discovery_service.common_ports
        assert 1883 in self.discovery_service.common_ports  # MQTT
    
    def test_jarvis_identifiers_structure(self):
        """Test that Jarvis identifiers are properly defined"""
        assert hasattr(self.discovery_service, 'jarvis_identifiers')
        assert isinstance(self.discovery_service.jarvis_identifiers, dict)
        assert 'hostname_patterns' in self.discovery_service.jarvis_identifiers
        assert 'services' in self.discovery_service.jarvis_identifiers
        assert 'ports' in self.discovery_service.jarvis_identifiers


def main():
    """Run the network discovery test manually"""
    print("🔍 Smart Device Network Discovery Test")
    print("=" * 50)
    print("Testing network discovery service functionality...")
    print()
    
    # Get the discovery service
    discovery_service = get_network_discovery_service()
    
    # Test basic functionality without network operations
    print("✅ Testing service initialization...")
    assert discovery_service is not None
    
    print("✅ Testing discovery summary structure...")
    summary = discovery_service.get_discovery_summary()
    print(f"   Total devices: {summary['total_devices']}")
    print(f"   Smart devices: {summary['smart_devices']}")
    print(f"   Jarvis nodes: {summary['jarvis_nodes']}")
    
    print("✅ Testing device detection...")
    jarvis_nodes = discovery_service.get_jarvis_nodes()
    smart_devices = discovery_service.get_smart_devices()
    print(f"   Jarvis nodes found: {len(jarvis_nodes)}")
    print(f"   Smart devices found: {len(smart_devices)}")
    
    print("✅ Testing DiscoveredDevice structure...")
    test_device = DiscoveredDevice(
        ip_address="192.168.1.100",
        hostname="test-device",
        device_type="smart_plug"
    )
    print(f"   Test device created: {test_device.ip_address}")
    
    print("\n✅ All basic tests passed!")
    print("\n💡 Note: This test only validates the service structure.")
    print("   For full network scanning, use the service directly.")


if __name__ == "__main__":
    main() 