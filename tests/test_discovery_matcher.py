import json

FINGERPRINTS_PATH = "runtime_fingerprint_index.json"

fake_device = {
    "mac": "A4:C1:38:12:34:56",
    "hostname": "esphome-bedroom",
    "mdns_type": "_esphomelib._tcp.local.",
    "ssdp": {
        "manufacturer": "Unknown",
        "modelName": ""
    }
}

def match_device(device_info, fingerprints):
    for fp in fingerprints:
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

def main():
    with open(FINGERPRINTS_PATH, "r") as f:
        fingerprints = json.load(f)
    match = match_device(fake_device, fingerprints)
    if match and match["integration"] == "esphome":
        print("✅ Esphome matched")
    else:
        print("❌ Esphome not matched")

if __name__ == "__main__":
    main() 