#!/usr/bin/env python3
"""
Test LG TV integration with various device scenarios
"""

import json
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from jarvis_integrations.lg_tv import LGTvIntegration


class TestLgTvIntegration:
    def setup_method(self):
        self.integration = LGTvIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "lg_tv"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_lg_mac_prefixes(self):
        """Test matching LG MAC prefixes"""
        lg_macs = [
            "A0:AB:1B:12:34:56"  # LG Electronics (only supported prefix)
        ]
        
        for mac in lg_macs:
            device_info = {"mac": mac}
            assert self.integration.matches(device_info), f"Should match MAC: {mac}"

    def test_matches_lg_hostnames(self):
        """Test matching LG hostnames"""
        lg_hostnames = [
            "lg-tv",
            "lg-smart-tv",
            "lg-oled-tv",
            "lg-webos-tv",
            "lg-tv-2023"
        ]
        
        for hostname in lg_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_lg_ssdp(self):
        """Test matching LG SSDP data"""
        lg_ssdp_devices = [
            {
                "ssdp": {
                    "manufacturer": "LG Electronics",
                    "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "lg electronics",
                    "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"
                }
            }
        ]
        
        for device_info in lg_ssdp_devices:
            assert self.integration.matches(device_info), f"Should match SSDP: {device_info}"

    def test_does_not_match_non_lg(self):
        """Test that non-LG devices don't match"""
        non_lg_devices = [
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "samsung-tv"},
            {"mac": "11:22:33:44:55:66", "hostname": "philips-hue"},
            {"mac": "FF:EE:DD:CC:BB:AA", "hostname": "sony-tv"},
            {"hostname": "apple-tv"},
            {"hostname": "fire-tv-stick"}
        ]
        
        for device_info in non_lg_devices:
            assert not self.integration.matches(device_info), f"Should not match: {device_info}"

    def test_enrich_device_info(self):
        """Test device enrichment"""
        device_info = {"hostname": "lg-tv"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "LG Electronics"
        assert enriched["category"] == "Smart TV"
        assert enriched["device_type"] == "lg_tv"

    def test_enrich_device_info_no_hostname(self):
        """Test device enrichment without hostname"""
        device_info = {"mac": "A0:AB:1B:12:34:56"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "LG Electronics"
        assert enriched["category"] == "Smart TV"
        assert enriched["device_type"] == "lg_tv"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_lg_tv" in commands

    def test_complex_device_matching(self):
        """Test complex device with multiple identifiers"""
        complex_device = {
            "mac": "A0:AB:1B:12:34:56",
            "hostname": "lg-oled-tv-living-room",
            "ssdp": {
                "manufacturer": "LG Electronics",
                "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1",
                "modelName": "LG OLED TV"
            }
        }
        
        assert self.integration.matches(complex_device)
        enriched = self.integration.enrich(complex_device)
        assert enriched["device_type"] == "lg_tv"

    def test_edge_case_hostnames(self):
        """Test edge case hostnames"""
        edge_hostnames = [
            "lg-tv-2023",
            "lg-oled-tv-65",
            "lg-webos-tv-pro",
            "lg-smart-tv-4k"
        ]
        
        for hostname in edge_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match edge case: {hostname}"

    def test_lg_tv_variations(self):
        """Test different LG TV variations"""
        lg_tv_variations = [
            "lg-tv",
            "lg-oled-tv",
            "lg-webos-tv",
            "lg-smart-tv",
            "lg-4k-tv",
            "lg-8k-tv"
        ]
        
        for variation in lg_tv_variations:
            device_info = {"hostname": variation}
            assert self.integration.matches(device_info), f"Should match variation: {variation}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__]) 