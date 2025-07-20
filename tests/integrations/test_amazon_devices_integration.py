#!/usr/bin/env python3
"""
Test Amazon device integrations with various device scenarios
"""

import json
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from jarvis_integrations.amazon_echo import AmazonEchoIntegration
from jarvis_integrations.amazon_fire_tv import AmazonFireTvIntegration
from jarvis_integrations.amazon_kindle import AmazonKindleIntegration


class TestAmazonEchoIntegration:
    def setup_method(self):
        self.integration = AmazonEchoIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "amazon_echo"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_amazon_mac_prefixes(self):
        """Test matching Amazon MAC prefixes"""
        amazon_macs = [
            "EC:8A:C4:12:34:56",  # Amazon Technologies Inc.
            "2A:5F:4C:AB:CD:EF",  # Amazon Technologies Inc.
            "44:65:0D:78:90:12",  # Amazon Technologies Inc.
            "F0:D2:F1:34:56:78",  # Amazon Technologies Inc.
            "6C:56:97:90:12:34"   # Amazon Technologies Inc.
        ]
        
        for mac in amazon_macs:
            device_info = {"mac": mac}
            assert self.integration.matches(device_info), f"Should match MAC: {mac}"

    def test_matches_amazon_hostnames(self):
        """Test matching Amazon Echo hostnames"""
        amazon_hostnames = [
            "amzn-echo-dot",
            "echo-show",
            "alexa-device",
            "amzn-echo",
            "echo-dot-3rd-gen"
        ]
        
        for hostname in amazon_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_amazon_ssdp(self):
        """Test matching Amazon SSDP data"""
        amazon_ssdp_devices = [
            {
                "ssdp": {
                    "manufacturer": "Amazon.com"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "amazon.com"
                }
            }
        ]
        
        for device_info in amazon_ssdp_devices:
            assert self.integration.matches(device_info), f"Should match SSDP: {device_info}"

    def test_matches_amazon_zeroconf(self):
        """Test matching Amazon Zeroconf services"""
        device_info = {
            "mdns_type": "_amzn-wplay._tcp.local."
        }
        assert self.integration.matches(device_info)

    def test_does_not_match_non_amazon(self):
        """Test that non-Amazon devices don't match"""
        non_amazon_devices = [
            {"mac": "AA:BB:CC:DD:EE:FF", "hostname": "samsung-tv"},
            {"mac": "11:22:33:44:55:66", "hostname": "philips-hue"},
            {"hostname": "apple-tv"},
            {"hostname": "google-home"}
        ]
        
        for device_info in non_amazon_devices:
            assert not self.integration.matches(device_info), f"Should not match: {device_info}"

    def test_enrich_device_info(self):
        """Test device enrichment"""
        device_info = {"hostname": "echo-show"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Amazon"
        assert enriched["category"] == "Smart Speaker"
        assert enriched["device_type"] == "amazon_echo"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_amazon_echo" in commands


class TestAmazonFireTvIntegration:
    def setup_method(self):
        self.integration = AmazonFireTvIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "amazon_fire_tv"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_fire_tv_hostnames(self):
        """Test matching Fire TV hostnames"""
        fire_tv_hostnames = [
            "aftv-stick",
            "firetv-cube",
            "aftv",
            "firetv-stick-4k",
            "aftv-cube-2nd-gen"
        ]
        
        for hostname in fire_tv_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_fire_tv_ssdp(self):
        """Test matching Fire TV SSDP data"""
        fire_tv_ssdp_devices = [
            {
                "ssdp": {
                    "manufacturer": "Amazon.com",
                    "modelName": "AFT*"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "Amazon.com",
                    "modelName": "Fire TV*"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "Amazon.com",
                    "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"
                }
            }
        ]
        
        for device_info in fire_tv_ssdp_devices:
            assert self.integration.matches(device_info), f"Should match SSDP: {device_info}"

    def test_matches_fire_tv_mac_with_context(self):
        """Test matching Fire TV by MAC with additional context"""
        # Test with MediaRenderer device type (Fire TV specific)
        device_info = {
            "mac": "EC:8A:C4:12:34:56",
            "ssdp": {
                "manufacturer": "Amazon.com",
                "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"
            }
        }
        assert self.integration.matches(device_info)

        # Test with Fire TV hostname
        device_info = {
            "mac": "2A:5F:4C:AB:CD:EF",
            "hostname": "aftv-stick"
        }
        assert self.integration.matches(device_info)

    def test_does_not_match_echo_devices(self):
        """Test that Echo devices don't match Fire TV"""
        echo_devices = [
            {"hostname": "amzn-echo-dot"},
            {"hostname": "echo-show"},
            {"hostname": "alexa-device"}
        ]
        
        for device_info in echo_devices:
            assert not self.integration.matches(device_info), f"Should not match Echo: {device_info}"

    def test_enrich_device_info(self):
        """Test device enrichment"""
        device_info = {"hostname": "aftv-stick"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Amazon"
        assert enriched["category"] == "Streaming Device"
        assert enriched["device_type"] == "amazon_fire_tv"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_amazon_fire_tv" in commands


class TestAmazonKindleIntegration:
    def setup_method(self):
        self.integration = AmazonKindleIntegration()

    def test_integration_name(self):
        """Test integration name"""
        assert self.integration.name == "amazon_kindle"

    def test_fingerprints_exist(self):
        """Test that fingerprints are defined"""
        fingerprints = self.integration.fingerprints
        assert isinstance(fingerprints, list)
        assert len(fingerprints) > 0

    def test_matches_kindle_hostnames(self):
        """Test matching Kindle hostnames"""
        kindle_hostnames = [
            "kindle-paperwhite",
            "kindle",
            "kindle-oasis",
            "kindle-voyage"
        ]
        
        for hostname in kindle_hostnames:
            device_info = {"hostname": hostname}
            assert self.integration.matches(device_info), f"Should match hostname: {hostname}"

    def test_matches_kindle_ssdp(self):
        """Test matching Kindle SSDP data"""
        kindle_ssdp_devices = [
            {
                "ssdp": {
                    "manufacturer": "Amazon.com",
                    "modelName": "Kindle*"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "Amazon.com",
                    "modelName": "Fire*"
                }
            },
            {
                "ssdp": {
                    "manufacturer": "Amazon.com",
                    "modelName": "Kindle Paperwhite*"
                }
            }
        ]
        
        for device_info in kindle_ssdp_devices:
            assert self.integration.matches(device_info), f"Should match SSDP: {device_info}"

    def test_matches_kindle_mac_with_context(self):
        """Test matching Kindle by MAC with additional context"""
        device_info = {
            "mac": "EC:8A:C4:12:34:56",
            "hostname": "kindle-paperwhite"
        }
        assert self.integration.matches(device_info)

    def test_does_not_match_non_kindle(self):
        """Test that non-Kindle devices don't match"""
        non_kindle_devices = [
            {"hostname": "amzn-echo-dot"},
            {"hostname": "aftv-stick"},
            {"hostname": "apple-ipad"},
            {"hostname": "samsung-tablet"}
        ]
        
        for device_info in non_kindle_devices:
            assert not self.integration.matches(device_info), f"Should not match: {device_info}"

    def test_enrich_device_info(self):
        """Test device enrichment"""
        device_info = {"hostname": "kindle-paperwhite"}
        enriched = self.integration.enrich(device_info)
        
        assert enriched["manufacturer"] == "Amazon"
        assert enriched["category"] == "E-Reader"
        assert enriched["device_type"] == "amazon_kindle"

    def test_get_commands(self):
        """Test available commands"""
        commands = self.integration.get_commands()
        assert isinstance(commands, list)
        assert "control_amazon_kindle" in commands


if __name__ == "__main__":
    import pytest
    pytest.main([__file__]) 