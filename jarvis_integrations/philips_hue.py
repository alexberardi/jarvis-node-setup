from core.ijarvis_integration import IJarvisIntegration

class PhilipsHueIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "philips_hue"

    @property
    def fingerprints(self):
        return [
            # Home Assistant verified patterns
            {
                "zeroconf": {"type": "_hue._tcp.local."}
            },
            # MAC address patterns (verified)
            {
                "dhcp": {"macaddress": "EC:B5:FA"},
                "ssdp": {"manufacturer": "Philips Lighting BV"}
            },
            {
                "dhcp": {"macaddress": "EC:B5:FA"},
                "zeroconf": {"type": "_hue._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "00:17:88"},
                "ssdp": {"manufacturer": "Philips Lighting BV"}
            },
            {
                "dhcp": {"macaddress": "00:17:88"},
                "zeroconf": {"type": "_hue._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "00:1B:63"},
                "ssdp": {"manufacturer": "Philips Lighting BV"}
            },
            {
                "dhcp": {"macaddress": "00:1B:63"},
                "zeroconf": {"type": "_hue._tcp.local."}
            },
            # Hostname patterns
            {
                "dhcp": {"hostname": "philips-hue"},
                "ssdp": {"manufacturer": "Philips Lighting BV"}
            },
            {
                "dhcp": {"hostname": "hue-bridge"},
                "ssdp": {"manufacturer": "Philips Lighting BV"}
            },
            # Fallback patterns
            {
                "ssdp": {"manufacturer": "Philips Lighting BV"}
            }
        ]

    def matches(self, device_info):
        # Check for Philips Hue by MAC prefix (verified)
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("ecb5fa") or mac.startswith("001788") or mac.startswith("001b63"):
            return True
        
        # Check for Philips Hue in hostname
        hostname = device_info.get("hostname", "").lower()
        if "philips" in hostname or "hue" in hostname:
            return True
            
        # Check for Philips in SSDP (Home Assistant verified)
        ssdp = device_info.get("ssdp", {})
        if "philips" in ssdp.get("manufacturer", "").lower():
            return True
            
        # Check for Hue service (Home Assistant verified)
        mdns_type = device_info.get("mdns_type", "")
        if "_hue._tcp.local." in mdns_type:
            return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "hue" in hostname:
            return {
                "manufacturer": "Philips Lighting BV",
                "category": "Smart Lighting",
                "device_type": "philips_hue"
            }
        else:
            return {
                "manufacturer": "Philips Lighting BV",
                "category": "Smart Lighting",
                "device_type": "philips_hue"
            }

    def get_commands(self):
        return ["control_philips_hue"] 