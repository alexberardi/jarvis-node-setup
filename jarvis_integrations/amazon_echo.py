from core.ijarvis_integration import IJarvisIntegration

class AmazonEchoIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "amazon_echo"

    @property
    def fingerprints(self):
        return [
            {
                "dhcp": {"macaddress": "EC:8A:C4"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"macaddress": "EC:8A:C4"},
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "2A:5F:4C"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"macaddress": "2A:5F:4C"},
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "44:65:0D"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"macaddress": "44:65:0D"},
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "F0:D2:F1"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"macaddress": "F0:D2:F1"},
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "6C:56:97"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"macaddress": "6C:56:97"},
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            },
            {
                "dhcp": {"hostname": "amzn-"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"hostname": "echo-"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"}
            },
            {
                "zeroconf": {"type": "_amzn-wplay._tcp.local."}
            }
        ]

    def matches(self, device_info):
        # Check for Amazon Echo by MAC prefix (but be more specific)
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("ec8ac4") or mac.startswith("2a5f4c") or mac.startswith("44650d") or mac.startswith("f0d2f1") or mac.startswith("6c5697"):
            # Additional check to differentiate from Fire TV
            hostname = device_info.get("hostname", "").lower()
            if "aftv" in hostname or "firetv" in hostname:
                return False  # This is a Fire TV, not an Echo
            return True
        
        # Check for Amazon Echo specific hostnames
        hostname = device_info.get("hostname", "").lower()
        if "amzn-echo" in hostname or "echo-" in hostname or "alexa" in hostname:
            return True
            
        # Check for Amazon in SSDP (but exclude Fire TV specific patterns)
        ssdp = device_info.get("ssdp", {})
        if "amazon.com" in ssdp.get("manufacturer", "").lower():
            # Check if it's a Fire TV by device type
            if ssdp.get("deviceType") == "urn:schemas-upnp-org:device:MediaRenderer:1":
                return False  # This is likely a Fire TV
            return True
            
        # Check for Amazon Echo service
        mdns_type = device_info.get("mdns_type", "")
        if "_amzn-wplay._tcp.local." in mdns_type:
            # Additional check to differentiate from Fire TV
            hostname = device_info.get("hostname", "").lower()
            if "aftv" in hostname or "firetv" in hostname:
                return False  # This is a Fire TV, not an Echo
            return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "echo" in hostname:
            return {
                "manufacturer": "Amazon",
                "category": "Smart Speaker",
                "device_type": "amazon_echo"
            }
        else:
            return {
                "manufacturer": "Amazon",
                "category": "Smart Speaker",
                "device_type": "amazon_echo"
            }

    def get_commands(self):
        return ["control_amazon_echo"] 