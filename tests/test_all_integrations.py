import json

FINGERPRINTS_PATH = "runtime_fingerprint_index.json"

# Helper: load fingerprints
with open(FINGERPRINTS_PATH, "r") as f:
    FINGERPRINTS = json.load(f)

def match_device(device_info):
    for fp in FINGERPRINTS:
        match = fp["match"]
        # MAC prefix
        if "dhcp" in match and "macaddress" in match["dhcp"]:
            mac_prefix = match["dhcp"]["macaddress"].lower().replace(":", "")
            if device_info.get("mac") and device_info["mac"].lower().replace(":", "").startswith(mac_prefix):
                return fp
        # Hostname prefix
        if "dhcp" in match and "hostname" in match["dhcp"]:
            if device_info.get("hostname") and match["dhcp"]["hostname"].lower() in device_info["hostname"].lower():
                return fp
        # Zeroconf type
        if "zeroconf" in match and "type" in match["zeroconf"]:
            if device_info.get("mdns_type") == match["zeroconf"]["type"]:
                return fp
        # SSDP fields
        if "ssdp" in match:
            ssdp = device_info.get("ssdp", {})
            all_match = True
            for k, v in match["ssdp"].items():
                if ssdp.get(k, "").lower() != v.lower():
                    all_match = False
                    break
            if all_match and match["ssdp"]:
                return fp
    return None

def test_all_integrations():
    print("🧪 Testing All Integrations")
    print("=" * 60)
    
    # Test devices based on your network
    test_devices = [
        {
            "name": "Nest Thermostat",
            "device": {
                "mac": "18:B4:30:12:34:56",
                "hostname": "nest-thermostat",
                "mdns_type": "_nest._tcp.local.",
                "ssdp": {"manufacturer": "Google"}
            },
            "expected": "nest"
        },
        {
            "name": "Roomba",
            "device": {
                "mac": "50:14:79:12:34:56", 
                "hostname": "roomba-living-room",
                "mdns_type": "_http._tcp.local.",
                "ssdp": {"manufacturer": "iRobot"}
            },
            "expected": "roomba"
        },
        {
            "name": "LG TV",
            "device": {
                "mac": "A0:AB:1B:12:34:56",
                "hostname": "lg-webos-tv",
                "mdns_type": "_webostv._tcp.local.",
                "ssdp": {"manufacturer": "LG Electronics"}
            },
            "expected": "lg_tv"
        },
        {
            "name": "Samsung TV",
            "device": {
                "mac": "00:1E:7D:12:34:56",
                "hostname": "samsung-tv",
                "mdns_type": "_samsungtv._tcp.local.",
                "ssdp": {"manufacturer": "Samsung Electronics"}
            },
            "expected": "samsung_tv"
        },
        {
            "name": "ESPHome Device",
            "device": {
                "mac": "A4:C1:38:12:34:56",
                "hostname": "esphome-bedroom",
                "mdns_type": "_esphomelib._tcp.local.",
                "ssdp": {"manufacturer": "Unknown"}
            },
            "expected": "esphome"
        },
        {
            "name": "Philips Hue Bridge",
            "device": {
                "mac": "00:17:88:12:34:56",
                "hostname": "philips-hue-bridge",
                "mdns_type": "_http._tcp.local.",
                "ssdp": {"manufacturer": "Philips", "deviceType": "urn:schemas-upnp-org:device:Basic:1"}
            },
            "expected": "philips_hue"
        }
    ]
    
    passed = 0
    total = len(test_devices)
    
    for test in test_devices:
        print(f"\n🔍 Testing {test['name']}:")
        match = match_device(test['device'])
        
        if match and match["integration"] == test["expected"]:
            print(f"   ✅ {test['name']} matched correctly to {match['integration']}")
            passed += 1
        elif match:
            print(f"   ❌ {test['name']} matched to wrong integration: {match['integration']} (expected {test['expected']})")
        else:
            print(f"   ❌ {test['name']} not matched (expected {test['expected']})")
    
    print(f"\n📊 Results: {passed}/{total} tests passed")
    
    if passed == total:
        print("🎉 All integrations working perfectly!")
    else:
        print("⚠️ Some integrations need attention")
    
    # Show all available fingerprints
    print(f"\n📋 Available fingerprints ({len(FINGERPRINTS)} total):")
    integrations = {}
    for fp in FINGERPRINTS:
        integration = fp["integration"]
        if integration not in integrations:
            integrations[integration] = 0
        integrations[integration] += 1
    
    for integration, count in integrations.items():
        print(f"   • {integration}: {count} fingerprint(s)")

if __name__ == "__main__":
    test_all_integrations() 