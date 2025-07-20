import asyncio
import json
import pytest
from aiodiscover import DiscoverHosts

FINGERPRINTS_PATH = "runtime_fingerprint_index.json"

# Helper: load fingerprints
with open(FINGERPRINTS_PATH, "r") as f:
    FINGERPRINTS = json.load(f)

def build_device_info(discovery_result):
    # Build a device info dict from discovery result
    info = {
        "ip": discovery_result.get("ip", ""),
        "hostname": discovery_result.get("hostname", ""),
        "mac": discovery_result.get("mac", ""),
        "mdns_type": discovery_result.get("type", ""),
        "ssdp": discovery_result.get("ssdp", {}),
    }
    return info

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

@pytest.mark.asyncio
async def test_discovery():
    """Test device discovery functionality"""
    print("🔍 Testing device discovery...")
    
    # Use aiodiscover's DiscoverHosts
    discover = DiscoverHosts()
    print("  Starting discovery...")
    results = await discover.async_discover()
    print(f"  Discovery complete, found {len(results)} results")
    
    # Show raw results
    print("\n📋 Raw discovery results:")
    for i, result in enumerate(results):
        print(f"  {i+1}. {result}")
    
    # Build device info dicts
    devices = []
    for result in results:
        devices.append(build_device_info(result))
    
    print(f"\n🏠 Processed {len(devices)} devices:")
    for device in devices:
        print(f"  Device: {device.get('ip')} - {device.get('hostname')} - {device.get('mac')}")
        match = match_device(device)
        if match:
            print(f"    ✅ Matched with integration '{match['integration']}'")
        else:
            print(f"    ❌ No integration match")
    
    # Basic assertions
    assert isinstance(results, list)
    assert isinstance(devices, list)
    assert len(devices) == len(results)

if __name__ == "__main__":
    asyncio.run(test_discovery()) 