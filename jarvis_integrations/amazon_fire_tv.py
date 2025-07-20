from core.ijarvis_integration import IJarvisIntegration

class AmazonFireTvIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "amazon_fire_tv"

    @property
    def fingerprints(self):
        return [
            # Home Assistant verified patterns
            {
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            # MAC address patterns (verified from your network)
            {
                "dhcp": {"macaddress": "EC:8A:C4"},
                "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"}
            },
            {
                "dhcp": {"macaddress": "EC:8A:C4"},
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "2A:5F:4C"},
                "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"}
            },
            {
                "dhcp": {"macaddress": "2A:5F:4C"},
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            # Fire TV specific hostname patterns (high priority)
            {
                "dhcp": {"hostname": "aftv-"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"hostname": "firetv-"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"hostname": "aftv"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            # Fire TV specific SSDP patterns
            {
                "ssdp": {"manufacturer": "Amazon.com", "modelName": "AFT*"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "modelName": "Fire TV*"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1", "modelName": "AFT*"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1", "modelName": "Fire TV*"}
            }
        ]

    def matches(self, device_info):
        # Check for Fire TV specific hostname patterns first (highest priority)
        hostname = device_info.get("hostname", "").lower()
        if "aftv" in hostname or "firetv" in hostname:
            return True
            
        # Check for Amazon Fire TV in SSDP with specific model names
        ssdp = device_info.get("ssdp", {})
        if "amazon.com" in ssdp.get("manufacturer", "").lower():
            model_name = ssdp.get("modelName", "").lower()
            if "aft" in model_name or "fire tv" in model_name:
                return True
            # Also match on deviceType alone (Fire TV specific)
            if ssdp.get("deviceType") == "urn:schemas-upnp-org:device:MediaRenderer:1":
                return True
            
        # Check for Amazon Fire TV by MAC prefix (your actual devices)
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("ec8ac4") or mac.startswith("2a5f4c"):
            # Additional check to differentiate from Echo
            if "aftv" in hostname or "firetv" in hostname:
                return True
            # Check for MediaRenderer device type (Fire TV specific)
            if ssdp.get("deviceType") == "urn:schemas-upnp-org:device:MediaRenderer:1":
                return True
            
        # Check for Amazon Fire TV service
        mdns_type = device_info.get("mdns_type", "")
        if "_amzn-wplay._tcp.local." in mdns_type:
            # Additional check to differentiate from Echo
            if "aftv" in hostname or "firetv" in hostname:
                return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "aftv" in hostname:
            return {
                "manufacturer": "Amazon",
                "category": "Streaming Device",
                "device_type": "amazon_fire_tv"
            }
        else:
            return {
                "manufacturer": "Amazon",
                "category": "Streaming Device",
                "device_type": "amazon_fire_tv"
            }

    def get_commands(self):
        return ["control_amazon_fire_tv"] 