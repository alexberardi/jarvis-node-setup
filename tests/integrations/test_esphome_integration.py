#!/usr/bin/env python3
"""
Test ESPHome integration with various device scenarios
"""

import json
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from jarvis_integrations.esphome import ESPHomeIntegration


class TestESPHomeIntegration:
    def setup_method(self):
        self.integration = ESPHomeIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "esphome"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_esphome_mac_prefixes(self):
        """Test matching ESPHome MAC prefixes"""
        esphome_macs = [
            "24:6F:28:12:34:56",  # ESP32
            "24:0A:C4:AB:CD:EF",  # ESP32
            "18:FE:34:78:90:12"   # ESP32
        ]
        
        for mac in esphome_macs:
            device_info = {"mac": mac}
            assert self.integration.matches(device_info), f"Should match MAC: {mac}"

    def test_matches_esphome_hostnames(self):
        """Test matching ESPHome hostnames"""
        esphome_hostnames = [
            "esphome-device",
            "esp32-sensor",
            "esp8266-relay",
            "esphome-thermostat",
            "esp32-camera"
        ]
        
        for hostname in esphome_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_esphome_zeroconf(self):
        """Test matching ESPHome Zeroconf services"""
        device_info = {
            "mdns_type": "_esphome._tcp.local."
        }
        assert self.integration.matches(device_info)

    def test_does_not_match_non_esphome(self):
        """Test that non-ESPHome devices don't match"""
        non_esphome_devices = [
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "samsung-tv"},
            {"mac": "11:22:33:44:55:66", "hostname": "philips-hue"},
            {"mac": "FF:EE:DD:CC:BB:AA", "hostname": "lg-tv"},
            {"hostname": "apple-tv"},
            {"hostname": "fire-tv-stick"}
        ]
        
        for device_info in non_esphome_devices:
            assert not self.integration.matches(device_info), f"Should not match: {device_info}"

    def test_enrich_device_info_esphome(self):
        """Test device enrichment for ESPHome devices"""
        device_info = {"hostname": "esphome-device"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "ESPHome"
        assert enriched["category"] == "IoT Device"
        assert enriched["device_type"] == "esphome"

    def test_enrich_device_info_esp32(self):
        """Test device enrichment for ESP32 devices"""
        device_info = {"hostname": "esp32-sensor"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Espressif"
        assert enriched["category"] == "IoT Device"
        assert enriched["device_type"] == "esphome"

    def test_enrich_device_info_no_hostname(self):
        """Test device enrichment without hostname"""
        device_info = {"mac": "24:6F:28:12:34:56"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "ESPHome"
        assert enriched["category"] == "IoT Device"
        assert enriched["device_type"] == "esphome"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_esphome" in commands

    def test_complex_device_matching(self):
        """Test complex device with multiple identifiers"""
        complex_device = {
            "mac": "24:6F:28:12:34:56",
            "hostname": "esphome-thermostat-living-room",
            "mdns_type": "_esphome._tcp.local."
        }
        
        assert self.integration.matches(complex_device)
        enriched = self.integration.enrich(complex_device)
        assert enriched["device_type"] == "esphome"

    def test_edge_case_hostnames(self):
        """Test edge case hostnames"""
        edge_hostnames = [
            "esphome-device-001",
            "esp32-sensor-2023",
            "esp8266-relay-pro",
            "esphome-thermostat-v2"
        ]
        
        for hostname in edge_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match edge case: {hostname}"

    def test_esp_device_variations(self):
        """Test different ESP device variations"""
        esp_devices = [
            "esp32",
            "esp8266",
            "esp32-s2",
            "esp32-c3",
            "esp32-s3"
        ]
        
        for device in esp_devices:
            device_info = {"hostname": f"{device}-sensor"}
            assert self.integration.matches(device_info), f"Should match device: {device}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__]) 