#!/usr/bin/env python3
"""
Test Roomba integration with various device scenarios
"""

import json
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from jarvis_integrations.roomba import RoombaIntegration


class TestRoombaIntegration:
    def setup_method(self):
        self.integration = RoombaIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "roomba"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_roomba_mac_prefixes(self):
        """Test matching Roomba MAC prefixes"""
        # Test all verified iRobot MAC prefixes
        roomba_macs = [
            "50:14:79:12:34:56",  # iRobot Roomba
            "00:12:37:AB:CD:EF"   # iRobot prefix
        ]
        
        for mac in roomba_macs:
            device_info = {"mac": mac}
            assert self.integration.matches(device_info), f"Should match MAC: {mac}"

    def test_matches_roomba_hostnames(self):
        """Test matching Roomba hostnames"""
        roomba_hostnames = [
            "roomba-vacuum",
            "roomba-980",
            "irobot-roomba",
            "roomba-i7",
            "irobot-980"
        ]
        
        for hostname in roomba_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_roomba_ssdp(self):
        """Test matching Roomba SSDP data"""
        roomba_ssdp_devices = [
            {
                "ssdp": {
                    "manufacturer": "iRobot"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "irobot"
                }
            }
        ]
        
        for device_info in roomba_ssdp_devices:
            assert self.integration.matches(device_info), f"Should match SSDP: {device_info}"

    def test_matches_roomba_zeroconf(self):
        """Test matching Roomba Zeroconf services"""
        device_info = {
            "mdns_type": "_irobot._tcp.local."
        }
        assert self.integration.matches(device_info)

    def test_does_not_match_non_roomba(self):
        """Test that non-Roomba devices don't match"""
        non_roomba_devices = [
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "samsung-tv"},
            {"mac": "11:22:33:44:55:66", "hostname": "philips-hue"},
            {"mac": "FF:EE:DD:CC:BB:AA", "hostname": "lg-tv"},
            {"hostname": "apple-tv"},
            {"hostname": "fire-tv-stick"}
        ]
        
        for device_info in non_roomba_devices:
            assert not self.integration.matches(device_info), f"Should not match: {device_info}"

    def test_enrich_device_info(self):
        """Test device enrichment"""
        device_info = {"hostname": "roomba-vacuum"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "iRobot"
        assert enriched["category"] == "Robot Vacuum"
        assert enriched["device_type"] == "roomba"

    def test_enrich_device_info_no_hostname(self):
        """Test device enrichment without hostname"""
        device_info = {"mac": "50:14:79:12:34:56"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "iRobot"
        assert enriched["category"] == "Robot Vacuum"
        assert enriched["device_type"] == "roomba"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_roomba" in commands

    def test_complex_device_matching(self):
        """Test complex device with multiple identifiers"""
        complex_device = {
            "mac": "50:14:79:F6:D9:C0",
            "hostname": "roomba-vacuum-living-room",
            "ssdp": {
                "manufacturer": "iRobot",
                "modelName": "Roomba i7"
            },
            "mdns_type": "_irobot._tcp.local."
        }
        
        assert self.integration.matches(complex_device)
        enriched = self.integration.enrich(complex_device)
        assert enriched["device_type"] == "roomba"

    def test_edge_case_hostnames(self):
        """Test edge case hostnames"""
        edge_hostnames = [
            "roomba-980-pro",
            "irobot-roomba-i7",
            "roomba-vacuum-2023",
            "irobot-980-plus"
        ]
        
        for hostname in edge_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match edge case: {hostname}"

    def test_roomba_model_variations(self):
        """Test different Roomba model variations"""
        roomba_models = [
            "roomba-960",
            "roomba-980",
            "roomba-i7",
            "roomba-i8",
            "roomba-j7",
            "irobot-960",
            "irobot-980",
            "irobot-i7"
        ]
        
        for model in roomba_models:
            device_info = {"hostname": model}
            assert self.integration.matches(device_info), f"Should match model: {model}"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__]) 