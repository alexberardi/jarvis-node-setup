#!/usr/bin/env python3
"""
Test Philips Hue integration with various device scenarios
"""

import json
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from jarvis_integrations.philips_hue import PhilipsHueIntegration


class TestPhilipsHueIntegration:
    def setup_method(self):
        self.integration = PhilipsHueIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "philips_hue"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_philips_mac_prefixes(self):
        """Test matching Philips MAC prefixes"""
        # Test all verified Philips MAC prefixes
        philips_macs = [
            "EC:B5:FA:12:34:56",  # Philips Lighting BV
            "00:17:88:AB:CD:EF",  # Philips prefix
            "00:1B:63:78:90:12"   # Philips prefix
        ]
        
        for mac in philips_macs:
            device_info = {"mac": mac}
            assert self.integration.matches(device_info), f"Should match MAC: {mac}"

    def test_matches_philips_hostnames(self):
        """Test matching Philips hostnames"""
        philips_hostnames = [
            "philips-hue",
            "philips-hue-bridge",
            "hue-bridge",
            "philips-hue-001",
            "hue-bridge-v2"
        ]
        
        for hostname in philips_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_philips_ssdp(self):
        """Test matching Philips SSDP data"""
        philips_ssdp_devices = [
            {
                "ssdp": {
                    "manufacturer": "Philips Lighting BV"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "philips lighting bv"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "Philips"
                }
            }
        ]
        
        for device_info in philips_ssdp_devices:
            assert self.integration.matches(device_info), f"Should match SSDP: {device_info}"

    def test_matches_philips_zeroconf(self):
        """Test matching Philips Zeroconf services"""
        device_info = {
            "mdns_type": "_hue._tcp.local."
        }
        assert self.integration.matches(device_info)

    def test_does_not_match_non_philips(self):
        """Test that non-Philips devices don't match"""
        non_philips_devices = [
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "samsung-tv"},
            {"mac": "11:22:33:44:55:66", "hostname": "lg-tv"},
            {"mac": "FF:EE:DD:CC:BB:AA", "hostname": "sony-tv"},
            {"hostname": "apple-tv"},
            {"hostname": "fire-tv-stick"}
        ]
        
        for device_info in non_philips_devices:
            assert not self.integration.matches(device_info), f"Should not match: {device_info}"

    def test_enrich_device_info(self):
        """Test device enrichment"""
        device_info = {"hostname": "philips-hue-bridge"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Philips Lighting BV"
        assert enriched["category"] == "Smart Lighting"
        assert enriched["device_type"] == "philips_hue"

    def test_enrich_device_info_no_hostname(self):
        """Test device enrichment without hostname"""
        device_info = {"mac": "EC:B5:FA:12:34:56"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Philips Lighting BV"
        assert enriched["category"] == "Smart Lighting"
        assert enriched["device_type"] == "philips_hue"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_philips_hue" in commands

    def test_complex_device_matching(self):
        """Test complex device with multiple identifiers"""
        complex_device = {
            "mac": "EC:B5:FA:1B:D9:AC",
            "hostname": "philips-hue-bridge-living-room",
            "ssdp": {
                "manufacturer": "Philips Lighting BV",
                "modelName": "Philips Hue Bridge"
            },
            "mdns_type": "_hue._tcp.local."
        }
        
        assert self.integration.matches(complex_device)
        enriched = self.integration.enrich(complex_device)
        assert enriched["device_type"] == "philips_hue"

    def test_edge_case_hostnames(self):
        """Test edge case hostnames"""
        edge_hostnames = [
            "philips-hue-bridge-v2",
            "hue-bridge-001",
            "philips-hue-2023",
            "hue-bridge-pro"
        ]
        
        for hostname in edge_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match edge case: {hostname}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__]) 