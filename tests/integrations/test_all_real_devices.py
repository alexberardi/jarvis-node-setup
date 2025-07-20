import json

FINGERPRINTS_PATH = "runtime_fingerprint_index.json"

# Helper: load fingerprints
with open(FINGERPRINTS_PATH, "r") as f:
    FINGERPRINTS = json.load(f)

def match_device(device_info):
    best_match = None
    best_score = 0
    
    for fp in FINGERPRINTS:
        match = fp["match"]
        score = 0
        
        # MAC prefix match (high priority)
        if "dhcp" in match and "macaddress" in match["dhcp"]:
            mac_prefix = match["dhcp"]["macaddress"].lower().replace(":", "")
            if device_info.get("mac") and device_info["mac"].lower().replace(":", "").startswith(mac_prefix):
                score += 10
        
        # Hostname prefix match (very high priority for specific devices)
        if "dhcp" in match and "hostname" in match["dhcp"]:
            if device_info.get("hostname") and match["dhcp"]["hostname"].lower() in device_info["hostname"].lower():
                score += 20  # Higher score for hostname matches
        
        # Zeroconf type match
        if "zeroconf" in match and "type" in match["zeroconf"]:
            if device_info.get("mdns_type") == match["zeroconf"]["type"]:
                score += 5
        
        # SSDP fields match (higher score for more specific matches)
        if "ssdp" in match:
            ssdp = device_info.get("ssdp", {})
            all_match = True
            ssdp_score = 0
            for k, v in match["ssdp"].items():
                if ssdp.get(k, "").lower() != v.lower():
                    all_match = False
                    break
                else:
                    ssdp_score += 1  # Score for each matching SSDP field
            
            if all_match and match["ssdp"]:
                score += ssdp_score * 3  # More SSDP fields = higher score
        
        # If this match has a higher score, it's the best match so far
        if score > best_score:
            best_score = score
            best_match = fp
    
    return best_match

def test_all_real_devices():
    print("🧪 Testing All Real Devices on Your Network")
    print("=" * 55)
    
    # Your actual devices from the network scan
    real_devices = [
        {
            "name": "Samsung TV",
            "ip": "10.0.0.82",
            "mac": "44:5C:E9:86:51:9C",
            "hostname": "samsung-tv",
            "ssdp": {"manufacturer": "Samsung Electronics", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"},
            "mdns_type": "_samsungtv._tcp.local."
        },
        {
            "name": "Roomba",
            "ip": "10.0.0.226",
            "mac": "50:14:79:F6:D9:C0",
            "hostname": "roomba-vacuum",
            "ssdp": {"manufacturer": "iRobot"}
        },
        {
            "name": "Philips Hue",
            "ip": "10.0.0.151",
            "mac": "EC:B5:FA:1B:D9:AC",
            "hostname": "philips-hue-bridge",
            "ssdp": {"manufacturer": "Philips Lighting BV"},
            "mdns_type": "_hue._tcp.local."
        },
        {
            "name": "Fire TV Cube",
            "ip": "10.0.0.176",
            "mac": "EC:8A:C4:19:6F:AE",
            "hostname": "aftv-cube",
            "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"},
            "mdns_type": "_amzn-wplay._tcp.local."
        },
        {
            "name": "Fire TV Stick",
            "ip": "10.0.0.240",
            "mac": "2A:5F:4C:CC:F3:A4",
            "hostname": "aftv-stick",
            "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"},
            "mdns_type": "_amzn-wplay._tcp.local."
        }
    ]
    
    print(f"Testing {len(real_devices)} devices from your network:")
    
    for device in real_devices:
        print(f"\n📱 {device['name']}:")
        print(f"   IP: {device['ip']}")
        print(f"   MAC: {device['mac']}")
        print(f"   Hostname: {device['hostname']}")
        
        match = match_device(device)
        
        if match:
            print(f"   ✅ Matched to: {match['integration']}")
            print(f"   📋 Match criteria: {match['match']}")
            
            # Show expected vs actual
            expected_integration = device['name'].lower().replace(' ', '_').replace('-', '_')
            if match["integration"] == expected_integration:
                print(f"   🎯 Perfect match!")
            else:
                print(f"   ⚠️  Expected: {expected_integration}, Got: {match['integration']}")
        else:
            print(f"   ❌ No match found")
    
    print(f"\n💡 Summary:")
    print(f"   - Samsung TV should match: samsung_tv")
    print(f"   - Roomba should match: roomba")
    print(f"   - Philips Hue should match: philips_hue")
    print(f"   - Fire TV devices should match: amazon_fire_tv")
    print(f"\n🔍 These tests use realistic hostnames and SSDP data!")

if __name__ == "__main__":
    test_all_real_devices() 