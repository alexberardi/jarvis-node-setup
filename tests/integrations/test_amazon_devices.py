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

def test_amazon_devices():
    print("🧪 Testing Amazon Device Integrations")
    print("=" * 50)
    
    # Test Amazon Echo devices
    print("\n📱 Testing Amazon Echo:")
    echo_devices = [
        {
            "ip": "10.0.0.100",
            "mac": "44:65:0D:12:34:56",
            "hostname": "amzn-echo-dot",
            "mdns_type": "_amzn-wplay._tcp.local.",
            "ssdp": {"manufacturer": "Amazon.com"}
        },
        {
            "ip": "10.0.0.101",
            "mac": "F0:D2:F1:AB:CD:EF",
            "hostname": "echo-show",
            "mdns_type": "_amzn-wplay._tcp.local.",
            "ssdp": {"manufacturer": "Amazon.com"}
        },
        {
            "ip": "10.0.0.102",
            "mac": "6C:56:97:78:90:12",
            "hostname": "alexa-device",
            "ssdp": {"manufacturer": "Amazon.com"}
        }
    ]
    
    for i, device in enumerate(echo_devices, 1):
        print(f"  Echo {i}: {device['ip']} ({device['hostname']})")
        match = match_device(device)
        if match and match["integration"] == "amazon_echo":
            print(f"    ✅ Matched to: {match['integration']}")
        else:
            print(f"    ❌ No match or wrong integration")
            if match:
                print(f"       Matched to: {match['integration']}")
    
    # Test Amazon Fire TV devices
    print("\n📺 Testing Amazon Fire TV:")
    firetv_devices = [
        {
            "ip": "10.0.0.200",
            "mac": "44:65:0D:34:56:78",
            "hostname": "aftv-stick",
            "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"}
        },
        {
            "ip": "10.0.0.201",
            "mac": "F0:D2:F1:BC:DE:F0",
            "hostname": "firetv-cube",
            "ssdp": {"manufacturer": "Amazon.com", "modelName": "AFT*"}
        },
        {
            "ip": "10.0.0.202",
            "mac": "6C:56:97:89:01:23",
            "hostname": "aftv",
            "ssdp": {"manufacturer": "Amazon.com", "modelName": "Fire TV*"}
        }
    ]
    
    for i, device in enumerate(firetv_devices, 1):
        print(f"  Fire TV {i}: {device['ip']} ({device['hostname']})")
        match = match_device(device)
        if match and match["integration"] == "amazon_fire_tv":
            print(f"    ✅ Matched to: {match['integration']}")
        else:
            print(f"    ❌ No match or wrong integration")
            if match:
                print(f"       Matched to: {match['integration']}")
    
    # Test Amazon Kindle devices
    print("\n📚 Testing Amazon Kindle:")
    kindle_devices = [
        {
            "ip": "10.0.0.300",
            "mac": "44:65:0D:56:78:90",
            "hostname": "kindle-paperwhite",
            "ssdp": {"manufacturer": "Amazon.com", "modelName": "Kindle*"}
        },
        {
            "ip": "10.0.0.301",
            "mac": "F0:D2:F1:CD:EF:01",
            "hostname": "kindle",
            "ssdp": {"manufacturer": "Amazon.com", "modelName": "Fire*"}
        },
        {
            "ip": "10.0.0.302",
            "mac": "6C:56:97:90:12:34",
            "hostname": "amzn-fire-tablet",
            "ssdp": {"manufacturer": "Amazon.com"}
        }
    ]
    
    for i, device in enumerate(kindle_devices, 1):
        print(f"  Kindle {i}: {device['ip']} ({device['hostname']})")
        match = match_device(device)
        if match and match["integration"] == "amazon_kindle":
            print(f"    ✅ Matched to: {match['integration']}")
        else:
            print(f"    ❌ No match or wrong integration")
            if match:
                print(f"       Matched to: {match['integration']}")
    
    # Test edge cases and conflicts
    print("\n🔍 Testing Edge Cases:")
    
    # Device that could match multiple Amazon integrations
    ambiguous_device = {
        "ip": "10.0.0.999",
        "mac": "44:65:0D:99:99:99",
        "hostname": "amzn-device",
        "ssdp": {"manufacturer": "Amazon.com"}
    }
    
    print(f"  Ambiguous device: {ambiguous_device['ip']} ({ambiguous_device['hostname']})")
    match = match_device(ambiguous_device)
    if match:
        print(f"    ✅ Matched to: {match['integration']}")
        print(f"    Note: This device could match multiple Amazon integrations")
    else:
        print(f"    ❌ No match found")

if __name__ == "__main__":
    test_amazon_devices() 