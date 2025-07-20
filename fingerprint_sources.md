# Device Fingerprint Sources

## 🏠 Home Assistant (Best Source)
**URL**: https://github.com/home-assistant/core/tree/dev/homeassistant/components
**Why**: Home Assistant has the most comprehensive device discovery system with thousands of integrations
**How to use**: 
- Browse component directories for `manifest.json` files
- Look for `dependencies` and `zeroconf` sections
- Check `config_flow.py` for discovery logic

### Key Home Assistant Discovery Files:
- **SSDP**: `manifest.json` → `zeroconf` section
- **DHCP**: `config_flow.py` → `async_step_dhcp()`
- **Zeroconf**: `config_flow.py` → `async_step_zeroconf()`

## 🔍 MAC Address Databases
**URL**: https://macvendors.com/api
**Why**: Official MAC address vendor assignments
**How to use**: API calls to identify manufacturers

### Popular MAC Vendors:
- **Amazon**: 44:65:0D, F0:D2:F1, 6C:56:97, EC:8A:C4, 2A:5F:4C
- **Samsung**: 44:5C:E9, 00:1E:7D, 00:07:AB, 00:16:32
- **Philips**: EC:B5:FA, 00:17:88, 00:1B:63
- **Nest**: 18:B4:30, 64:16:66, 18:B4:30
- **LG**: A0:AB:1B, 00:1E:A7, 00:1B:63

## 🌐 UPnP/SSDP Databases
**URL**: https://github.com/home-assistant/core/tree/dev/homeassistant/components
**Why**: Standard device discovery protocols
**How to use**: Look for `ssdp` sections in Home Assistant components

### Common SSDP Device Types:
- `urn:schemas-upnp-org:device:MediaRenderer:1` (TVs, speakers)
- `urn:schemas-upnp-org:device:Basic:1` (Basic devices)
- `urn:schemas-upnp-org:device:InternetGatewayDevice:1` (Routers)

## 📱 Zeroconf/mDNS Services
**URL**: https://github.com/home-assistant/core/tree/dev/homeassistant/components
**Why**: Local network service discovery
**How to use**: Look for `zeroconf` sections in Home Assistant components

### Common Zeroconf Services:
- `_amzn-wplay._tcp.local.` (Amazon devices)
- `_samsungtv._tcp.local.` (Samsung TVs)
- `_hue._tcp.local.` (Philips Hue)
- `_esphome._tcp.local.` (ESPHome devices)

## 🛠️ Network Scanner Tools
**URL**: https://github.com/home-assistant/core/tree/dev/homeassistant/components
**Why**: Real-world device discovery
**How to use**: Run network scans and analyze results

### Tools:
- **nmap**: `nmap -sn 10.0.0.0/24`
- **arp-scan**: `arp-scan --localnet`
- **aiodiscover**: Python library we're using

## 📊 Device Manufacturer APIs
**URL**: Various manufacturer APIs
**Why**: Official device identification
**How to use**: API calls to manufacturer endpoints

### Examples:
- **Philips Hue**: https://discovery.meethue.com/
- **Samsung**: SSDP discovery
- **Amazon**: Internal APIs (limited access)

## 🔧 How to Extract Fingerprints

### 1. From Home Assistant Components:
```bash
# Clone Home Assistant
git clone https://github.com/home-assistant/core.git
cd core/homeassistant/components

# Find discovery patterns
grep -r "zeroconf" . | grep -E "(amazon|samsung|philips|nest)"
grep -r "dhcp" . | grep -E "(macaddress|hostname)"
```

### 2. From Network Scans:
```python
# Use aiodiscover to scan network
from aiodiscover import DiscoverHosts

async def scan_network():
    async with DiscoverHosts() as scanner:
        devices = await scanner.async_discover()
        for device in devices:
            print(f"IP: {device.ip}")
            print(f"MAC: {device.mac}")
            print(f"Hostname: {device.hostname}")
            print(f"SSDP: {device.ssdp}")
            print(f"Zeroconf: {device.zeroconf}")
```

### 3. From MAC Vendor Lookups:
```python
import requests

def get_vendor(mac_prefix):
    url = f"https://api.macvendors.com/{mac_prefix}"
    response = requests.get(url)
    return response.text if response.status_code == 200 else "Unknown"
```

## 🎯 Recommended Approach

1. **Start with Home Assistant**: Extract fingerprints from existing integrations
2. **Validate with MAC databases**: Confirm manufacturer assignments
3. **Test with real devices**: Run discovery on your network
4. **Iterate and improve**: Add more specific fingerprints based on results

## 📁 File Structure for Fingerprints

```
jarvis_integrations/
├── __init__.py
├── amazon_echo.py          # Amazon Echo/Alexa devices
├── amazon_fire_tv.py       # Fire TV/Stick devices  
├── amazon_kindle.py        # Kindle/Fire tablets
├── samsung_tv.py           # Samsung Smart TVs
├── philips_hue.py          # Philips Hue lights
├── nest.py                 # Nest thermostats/cameras
├── lg_tv.py               # LG Smart TVs
├── roomba.py              # iRobot Roomba vacuums
└── esphome.py             # ESPHome devices
```

Each integration should include:
- MAC address prefixes
- Hostname patterns
- SSDP device types
- Zeroconf service types
- Model name patterns 