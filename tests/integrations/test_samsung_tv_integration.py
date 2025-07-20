#!/usr/bin/env python3
"""
Test Samsung TV integration with various device scenarios
"""

import json
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from jarvis_integrations.samsung_tv import SamsungTvIntegration


class TestSamsungTvIntegration:
    def setup_method(self):
        self.integration = SamsungTvIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "samsung_tv"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_samsung_mac_prefixes(self):
        """Test matching Samsung MAC prefixes"""
        # Test all verified Samsung MAC prefixes
        samsung_macs = [
            "44:5C:E9:12:34:56",  # Your Samsung TV
            "00:1E:7D:AB:CD:EF",  # Standard Samsung
            "00:07:AB:78:90:12",  # Samsung prefix
            "00:16:32:34:56:78"   # Samsung prefix
        ]
        
        for mac in samsung_macs:
            device_info = {"mac": mac}
            assert self.integration.matches(device_info), f"Should match MAC: {mac}"

    def test_matches_samsung_hostnames(self):
        """Test matching Samsung hostnames"""
        samsung_hostnames = [
            "samsung-tv",
            "samsung-smart-tv",
            "samsung-4k-tv",
            "samsung-oled-tv",
            "samsung-tv-2023"
        ]
        
        for hostname in samsung_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_samsung_ssdp(self):
        """Test matching Samsung SSDP data"""
        samsung_ssdp_devices = [
            {
                "ssdp": {
                    "manufacturer": "Samsung Electronics",
                    "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "samsung electronics",
                    "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "Samsung"
                }
            }
        ]
        
        for device_info in samsung_ssdp_devices:
            assert self.integration.matches(device_info), f"Should match SSDP: {device_info}"

    def test_matches_samsung_zeroconf(self):
        """Test matching Samsung Zeroconf services"""
        device_info = {
            "mdns_type": "_samsungtv._tcp.local."
        }
        assert self.integration.matches(device_info)

    def test_does_not_match_non_samsung(self):
        """Test that non-Samsung devices don't match"""
        non_samsung_devices = [
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "lg-tv"},
            {"mac": "11:22:33:44:55:66", "hostname": "philips-tv"},
            {"mac": "FF:EE:DD:CC:BB:AA", "hostname": "sony-tv"},
            {"hostname": "apple-tv"},
            {"hostname": "fire-tv-stick"}
        ]
        
        for device_info in non_samsung_devices:
            assert not self.integration.matches(device_info), f"Should not match: {device_info}"

    def test_enrich_device_info(self):
        """Test device enrichment"""
        device_info = {"hostname": "samsung-tv"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Samsung Electronics"
        assert enriched["category"] == "Smart TV"
        assert enriched["device_type"] == "samsung_tv"

    def test_enrich_device_info_no_hostname(self):
        """Test device enrichment without hostname"""
        device_info = {"mac": "44:5C:E9:12:34:56"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Samsung Electronics"
        assert enriched["category"] == "Smart TV"
        assert enriched["device_type"] == "samsung_tv"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_samsung_tv" in commands

    def test_complex_device_matching(self):
        """Test complex device with multiple identifiers"""
        complex_device = {
            "mac": "44:5C:E9:86:51:9C",
            "hostname": "samsung-tv-living-room",
            "ssdp": {
                "manufacturer": "Samsung Electronics",
                "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1",
                "modelName": "Samsung Smart TV"
            },
            "mdns_type": "_samsungtv._tcp.local."
        }
        
        assert self.integration.matches(complex_device)
        enriched = self.integration.enrich(complex_device)
        assert enriched["device_type"] == "samsung_tv"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__]) 