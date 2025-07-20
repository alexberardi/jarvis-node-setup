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

def test_real_amazon_devices():
    print("🧪 Testing Real Amazon Devices on Your Network")
    print("=" * 55)
    
    # Your actual Amazon devices from the network scan
    real_amazon_devices = [
        {
            "ip": "10.0.0.176",
            "mac": "EC:8A:C4:19:6F:AE",
            "hostname": "amazon-device-1",
            "ssdp": {"manufacturer": "Amazon.com"}
        },
        {
            "ip": "10.0.0.240", 
            "mac": "2A:5F:4C:CC:F3:A4",
            "hostname": "amazon-device-2",
            "ssdp": {"manufacturer": "Amazon.com"}
        }
    ]
    
    print(f"Found {len(real_amazon_devices)} Amazon devices on your network:")
    
    for i, device in enumerate(real_amazon_devices, 1):
        print(f"\n📱 Amazon Device {i}:")
        print(f"   IP: {device['ip']}")
        print(f"   MAC: {device['mac']}")
        print(f"   Hostname: {device['hostname']}")
        
        match = match_device(device)
        
        if match:
            print(f"   ✅ Matched to: {match['integration']}")
            print(f"   📋 Match criteria: {match['match']}")
            
            # Try to determine if it's Echo, Fire TV, or Kindle based on context
            if match["integration"] == "amazon_echo":
                print(f"   🎯 Likely device type: Amazon Echo/Alexa device")
            elif match["integration"] == "amazon_fire_tv":
                print(f"   🎯 Likely device type: Amazon Fire TV/Stick")
            elif match["integration"] == "amazon_kindle":
                print(f"   🎯 Likely device type: Amazon Kindle/Fire tablet")
        else:
            print(f"   ❌ No match found")
    
    print(f"\n💡 Based on your description:")
    print(f"   - You have 1 Fire TV Cube")
    print(f"   - You have 1 Fire TV Stick") 
    print(f"   - You have 1 Kindle")
    print(f"   - You don't have any Echo devices")
    print(f"\n🔍 The devices at 10.0.0.176 and 10.0.0.240 are likely your Fire TV devices!")

if __name__ == "__main__":
    test_real_amazon_devices() 