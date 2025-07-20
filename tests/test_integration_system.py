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

def test_integration_system():
    print("🧪 Testing Integration System")
    print("=" * 50)
    
    # Test 1: ESPHome device (should match)
    print("\n1️⃣ Testing ESPHome device:")
    esphome_device = {
        "mac": "A4:C1:38:12:34:56",
        "hostname": "esphome-bedroom",
        "mdns_type": "_esphomelib._tcp.local.",
        "ssdp": {"manufacturer": "Unknown"}
    }
    match = match_device(esphome_device)
    if match and match["integration"] == "esphome":
        print("   ✅ ESPHome device matched correctly")
    else:
        print("   ❌ ESPHome device not matched")
    
    # Test 2: Philips Hue device (should match)
    print("\n2️⃣ Testing Philips Hue device:")
    hue_device = {
        "mac": "00:17:88:12:34:56",
        "hostname": "philips-hue-bridge",
        "mdns_type": "_http._tcp.local.",
        "ssdp": {"manufacturer": "Philips", "deviceType": "urn:schemas-upnp-org:device:Basic:1"}
    }
    match = match_device(hue_device)
    if match and match["integration"] == "philips_hue":
        print("   ✅ Philips Hue device matched correctly")
    else:
        print("   ❌ Philips Hue device not matched")
    
    # Test 3: Unknown device (should not match)
    print("\n3️⃣ Testing unknown device:")
    unknown_device = {
        "mac": "AA:BB:CC:12:34:56",
        "hostname": "random-device",
        "mdns_type": "_http._tcp.local.",
        "ssdp": {"manufacturer": "Unknown"}
    }
    match = match_device(unknown_device)
    if not match:
        print("   ✅ Unknown device correctly not matched")
    else:
        print(f"   ❌ Unknown device incorrectly matched to {match['integration']}")
    
    # Test 4: Show available fingerprints
    print("\n4️⃣ Available fingerprints:")
    for i, fp in enumerate(FINGERPRINTS):
        print(f"   {i+1}. {fp['integration']}: {fp['match']}")
    
    print("\n🎉 Integration system test complete!")

if __name__ == "__main__":
    test_integration_system() 