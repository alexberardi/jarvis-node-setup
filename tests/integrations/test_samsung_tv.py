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

def test_samsung_tv():
    print("🧪 Testing Samsung TV Integration")
    print("=" * 40)
    
    # Test with the actual Samsung TV from your network
    samsung_tv = {
        "ip": "10.0.0.82",
        "mac": "44:5C:E9:86:51:9C",
        "hostname": "samsung-tv",
        "mdns_type": "_samsungtv._tcp.local.",
        "ssdp": {"manufacturer": "Samsung Electronics"}
    }
    
    print(f"Testing Samsung TV at {samsung_tv['ip']}")
    print(f"MAC: {samsung_tv['mac']}")
    
    match = match_device(samsung_tv)
    
    if match and match["integration"] == "samsung_tv":
        print("✅ Samsung TV matched correctly!")
        print(f"   Integration: {match['integration']}")
        print(f"   Match criteria: {match['match']}")
    else:
        print("❌ Samsung TV not matched correctly")
        if match:
            print(f"   Matched to: {match['integration']}")
        else:
            print("   No match found")
    
    # Also test with the old MAC prefix to make sure it still works
    print(f"\n🔍 Testing with old Samsung MAC prefix (00:1E:7D):")
    old_samsung = {
        "ip": "10.0.0.100",
        "mac": "00:1E:7D:12:34:56",
        "hostname": "samsung-tv-old",
        "mdns_type": "_samsungtv._tcp.local.",
        "ssdp": {"manufacturer": "Samsung Electronics"}
    }
    
    match = match_device(old_samsung)
    if match and match["integration"] == "samsung_tv":
        print("✅ Old Samsung MAC prefix still works!")
    else:
        print("❌ Old Samsung MAC prefix broken")

if __name__ == "__main__":
    test_samsung_tv() 