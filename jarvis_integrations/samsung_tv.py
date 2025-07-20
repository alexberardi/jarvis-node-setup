from core.ijarvis_integration import IJarvisIntegration

class SamsungTvIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "samsung_tv"

    @property
    def fingerprints(self):
        return [
            # Home Assistant verified patterns
            {
                "zeroconf": {
                    "type": "_samsungtv._tcp.local.",
                    "properties": {
                        "manufacturer": "samsung*"
                    }
                }
            },
            {
                "ssdp": {
                    "manufacturer": "samsung*",
                    "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"
                }
            },
            # MAC address patterns (verified)
            {
                "dhcp": {"macaddress": "44:5C:E9"},
                "ssdp": {"manufacturer": "Samsung Electronics"}
            },
            {
                "dhcp": {"macaddress": "44:5C:E9"},
                "zeroconf": {"type": "_samsungtv._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "00:1E:7D"},
                "ssdp": {"manufacturer": "Samsung Electronics"}
            },
            {
                "dhcp": {"macaddress": "00:1E:7D"},
                "zeroconf": {"type": "_samsungtv._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "00:07:AB"},
                "ssdp": {"manufacturer": "Samsung Electronics"}
            },
            {
                "dhcp": {"macaddress": "00:16:32"},
                "ssdp": {"manufacturer": "Samsung Electronics"}
            },
            # Fallback patterns
            {
                "ssdp": {"manufacturer": "Samsung Electronics", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"}
            },
            {
                "zeroconf": {"type": "_samsungtv._tcp.local."}
            }
        ]

    def matches(self, device_info):
        # Check for Samsung TV by MAC prefix (verified)
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("445ce9") or mac.startswith("001e7d") or mac.startswith("0007ab") or mac.startswith("001632"):
            return True
        
        # Check for Samsung in hostname (more specific)
        hostname = device_info.get("hostname", "").lower()
        if "samsung" in hostname:
            return True
            
        # Check for Samsung in SSDP (Home Assistant verified)
        ssdp = device_info.get("ssdp", {})
        if "samsung" in ssdp.get("manufacturer", "").lower():
            return True
            
        # Check for Samsung TV service (Home Assistant verified)
        mdns_type = device_info.get("mdns_type", "")
        if "_samsungtv._tcp.local." in mdns_type:
            return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "samsung" in hostname:
            return {
                "manufacturer": "Samsung Electronics",
                "category": "Smart TV",
                "device_type": "samsung_tv"
            }
        else:
            return {
                "manufacturer": "Samsung Electronics",
                "category": "Smart TV",
                "device_type": "samsung_tv"
            }

    def get_commands(self):
        return ["control_samsung_tv"] 