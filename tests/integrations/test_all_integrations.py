#!/usr/bin/env python3
"""
Comprehensive test runner for all device integrations
"""

import json
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), '..', '..'))

from jarvis_integrations.samsung_tv import SamsungTvIntegration
from jarvis_integrations.philips_hue import PhilipsHueIntegration
from jarvis_integrations.roomba import RoombaIntegration
from jarvis_integrations.esphome import ESPHomeIntegration
from jarvis_integrations.nest import NestIntegration
from jarvis_integrations.lg_tv import LGTvIntegration
from jarvis_integrations.amazon_echo import AmazonEchoIntegration
from jarvis_integrations.amazon_fire_tv import AmazonFireTvIntegration
from jarvis_integrations.amazon_kindle import AmazonKindleIntegration


class TestAllIntegrations:
    def setup_method(self):
        """Set up all integrations for testing"""
        self.integrations = {
            "samsung_tv": SamsungTvIntegration(),
            "philips_hue": PhilipsHueIntegration(),
            "roomba": RoombaIntegration(),
            "esphome": ESPHomeIntegration(),
            "nest": NestIntegration(),
            "lg_tv": LGTvIntegration(),
            "amazon_echo": AmazonEchoIntegration(),
            "amazon_fire_tv": AmazonFireTvIntegration(),
            "amazon_kindle": AmazonKindleIntegration()
        }

    def test_all_integrations_have_names(self):
        """Test that all integrations have proper names"""
        for name, integration in self.integrations.items():
            assert integration.name == name, f"Integration {name} has wrong name: {integration.name}"

    def test_all_integrations_have_fingerprints(self):
        """Test that all integrations have fingerprints defined"""
        for name, integration in self.integrations.items():
            fingerprints = integration.fingerprints
            assert isinstance(fingerprints, list), f"Integration {name} fingerprints not a list"
            assert len(fingerprints) > 0, f"Integration {name} has no fingerprints"

    def test_all_integrations_have_commands(self):
        """Test that all integrations have commands defined"""
        for name, integration in self.integrations.items():
            commands = integration.get_commands()
            assert isinstance(commands, list), f"Integration {name} commands not a list"
            assert len(commands) > 0, f"Integration {name} has no commands"

    def test_device_matching_accuracy(self):
        """Test that devices match the correct integration"""
        test_devices = [
            # Samsung TV devices
            {
                "name": "Samsung TV",
                "device_info": {
                    "mac": "44:5C:E9:12:34:56",
                    "hostname": "samsung-tv",
                    "ssdp": {"manufacturer": "Samsung Electronics"},
                    "mdns_type": "_samsungtv._tcp.local."
                },
                "expected_integration": "samsung_tv"
            },
            # Philips Hue devices
            {
                "name": "Philips Hue Bridge",
                "device_info": {
                    "mac": "EC:B5:FA:12:34:56",
                    "hostname": "philips-hue-bridge",
                    "ssdp": {"manufacturer": "Philips Lighting BV"},
                    "mdns_type": "_hue._tcp.local."
                },
                "expected_integration": "philips_hue"
            },
            # Roomba devices
            {
                "name": "Roomba Vacuum",
                "device_info": {
                    "mac": "50:14:79:12:34:56",
                    "hostname": "roomba-vacuum",
                    "ssdp": {"manufacturer": "iRobot"}
                },
                "expected_integration": "roomba"
            },
            # ESPHome devices
            {
                "name": "ESPHome Device",
                "device_info": {
                    "mac": "24:6F:28:12:34:56",
                    "hostname": "esphome-device",
                    "mdns_type": "_esphome._tcp.local."
                },
                "expected_integration": "esphome"
            },
            # Nest devices
            {
                "name": "Nest Thermostat",
                "device_info": {
                    "mac": "18:B4:30:12:34:56",
                    "hostname": "nest-thermostat",
                    "ssdp": {"manufacturer": "Nest Labs"}
                },
                "expected_integration": "nest"
            },
            # LG TV devices
            {
                "name": "LG TV",
                "device_info": {
                    "mac": "A0:AB:1B:12:34:56",
                    "hostname": "lg-tv",
                    "ssdp": {"manufacturer": "LG Electronics"},
                    "mdns_type": "_webostv._tcp.local."
                },
                "expected_integration": "lg_tv"
            },
            # Amazon Echo devices
            {
                "name": "Amazon Echo",
                "device_info": {
                    "mac": "44:65:0D:12:34:56",
                    "hostname": "amzn-echo-dot",
                    "ssdp": {"manufacturer": "Amazon.com"},
                    "mdns_type": "_amzn-wplay._tcp.local."
                },
                "expected_integration": "amazon_echo"
            },
            # Amazon Fire TV devices
            {
                "name": "Amazon Fire TV",
                "device_info": {
                    "mac": "EC:8A:C4:12:34:56",
                    "hostname": "aftv-stick",
                    "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"}
                },
                "expected_integration": "amazon_fire_tv"
            },
            # Amazon Kindle devices
            {
                "name": "Amazon Kindle",
                "device_info": {
                    "mac": "EC:8A:C4:12:34:56",
                    "hostname": "kindle-paperwhite",
                    "ssdp": {"manufacturer": "Amazon.com", "modelName": "Kindle*"}
                },
                "expected_integration": "amazon_kindle"
            }
        ]

        for test_device in test_devices:
            device_name = test_device["name"]
            device_info = test_device["device_info"]
            expected_integration = test_device["expected_integration"]
            
            # Use scoring system to find best match (like real discovery)
            best_match = None
            best_score = 0
            
            for name, integration in self.integrations.items():
                if integration.matches(device_info):
                    # Calculate score based on specificity
                    score = 0
                    
                    # Hostname matches get high score
                    hostname = device_info.get("hostname", "").lower()
                    if "samsung" in hostname and name == "samsung_tv":
                        score += 20
                    elif "philips" in hostname and name == "philips_hue":
                        score += 20
                    elif "roomba" in hostname and name == "roomba":
                        score += 20
                    elif "esphome" in hostname and name == "esphome":
                        score += 20
                    elif "nest" in hostname and name == "nest":
                        score += 20
                    elif "lg" in hostname and name == "lg_tv":
                        score += 20
                    elif "amzn-echo" in hostname and name == "amazon_echo":
                        score += 20
                    elif "aftv" in hostname and name == "amazon_fire_tv":
                        score += 20
                    elif "kindle" in hostname and name == "amazon_kindle":
                        score += 20
                    
                    # MAC prefix matches
                    mac = device_info.get("mac", "").lower().replace(":", "")
                    if name == "samsung_tv" and (mac.startswith("445ce9") or mac.startswith("001e7d")):
                        score += 10
                    elif name == "philips_hue" and (mac.startswith("ecb5fa") or mac.startswith("001788")):
                        score += 10
                    elif name == "roomba" and (mac.startswith("501479") or mac.startswith("001237")):
                        score += 10
                    elif name == "esphome" and (mac.startswith("246f28") or mac.startswith("240ac4")):
                        score += 10
                    elif name == "nest" and (mac.startswith("18b430") or mac.startswith("641666")):
                        score += 10
                    elif name == "lg_tv" and mac.startswith("a0ab1b"):
                        score += 10
                    elif name == "amazon_echo" and (mac.startswith("44650d") or mac.startswith("f0d2f1")):
                        score += 10
                    elif name == "amazon_fire_tv" and (mac.startswith("ec8ac4") or mac.startswith("2a5f4c")):
                        score += 10
                    elif name == "amazon_kindle" and (mac.startswith("ec8ac4") or mac.startswith("2a5f4c")):
                        score += 10
                    
                    # SSDP specific matches get higher score
                    ssdp = device_info.get("ssdp", {})
                    if name == "amazon_fire_tv" and ssdp.get("deviceType") == "urn:schemas-upnp-org:device:MediaRenderer:1":
                        score += 15
                    elif name == "amazon_kindle" and "kindle" in ssdp.get("modelName", "").lower():
                        score += 15
                    
                    # Base score for any match
                    score += 5
                    
                    if score > best_score:
                        best_score = score
                        best_match = name
            
            assert best_match == expected_integration, \
                f"{device_name} matched to {best_match} (score: {best_score}), expected {expected_integration}"

    def test_no_false_positives(self):
        """Test that integrations don't match unrelated devices"""
        unrelated_devices = [
            {
                "name": "Apple TV",
                "device_info": {
                    "mac": "AA:BB:CC:DD:EE:FF",
                    "hostname": "apple-tv",
                    "ssdp": {"manufacturer": "Apple Inc."}
                }
            },
            {
                "name": "Google Home",
                "device_info": {
                    "mac": "11:22:33:44:55:66",
                    "hostname": "google-home",
                    "ssdp": {"manufacturer": "Google Inc."}
                }
            },
            {
                "name": "Sony TV",
                "device_info": {
                    "mac": "FF:EE:DD:CC:BB:AA",
                    "hostname": "sony-tv",
                    "ssdp": {"manufacturer": "Sony Corporation"}
                }
            }
        ]

        for unrelated_device in unrelated_devices:
            device_name = unrelated_device["name"]
            device_info = unrelated_device["device_info"]
            
            # Check that no integration matches this unrelated device
            for name, integration in self.integrations.items():
                assert not integration.matches(device_info), \
                    f"Integration {name} incorrectly matched {device_name}"

    def test_device_enrichment(self):
        """Test that device enrichment works correctly"""
        test_cases = [
            {
                "integration": "samsung_tv",
                "device_info": {"hostname": "samsung-tv"},
                "expected": {
                    "manufacturer": "Samsung Electronics",
                    "category": "Smart TV",
                    "device_type": "samsung_tv"
                }
            },
            {
                "integration": "philips_hue",
                "device_info": {"hostname": "philips-hue-bridge"},
                "expected": {
                    "manufacturer": "Philips Lighting BV",
                    "category": "Smart Lighting",
                    "device_type": "philips_hue"
                }
            },
            {
                "integration": "roomba",
                "device_info": {"hostname": "roomba-vacuum"},
                "expected": {
                    "manufacturer": "iRobot",
                    "category": "Robot Vacuum",
                    "device_type": "roomba"
                }
            }
        ]

        for test_case in test_cases:
            integration_name = test_case["integration"]
            device_info = test_case["device_info"]
            expected = test_case["expected"]
            
            integration = self.integrations[integration_name]
            enriched = integration.enrich(device_info)
            
            for key, value in expected.items():
                assert enriched[key] == value, \
                    f"Integration {integration_name} enrichment failed for {key}: expected {value}, got {enriched[key]}"

    def test_fingerprint_consistency(self):
        """Test that fingerprints are consistent across integrations"""
        for name, integration in self.integrations.items():
            fingerprints = integration.fingerprints
            
            for fingerprint in fingerprints:
                # Check that fingerprint has required structure
                assert isinstance(fingerprint, dict), f"Integration {name} has non-dict fingerprint"
                
                # Check that fingerprint has at least one matching criteria
                has_criteria = False
                for criteria_type in ["dhcp", "ssdp", "zeroconf"]:
                    if criteria_type in fingerprint:
                        has_criteria = True
                        break
                
                assert has_criteria, f"Integration {name} has fingerprint without matching criteria"


if __name__ == "__main__":
    import pytest
    pytest.main([__file__]) 