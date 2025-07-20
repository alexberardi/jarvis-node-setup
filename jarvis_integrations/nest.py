from core.ijarvis_integration import IJarvisIntegration

class NestIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "nest"

    @property
    def fingerprints(self):
        return [
            {
                "dhcp": {"macaddress": "18:B4:30"},
                "zeroconf": {"type": "_nest._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "64:16:66"},
                "zeroconf": {"type": "_googlecast._tcp.local."}
            }
        ]

    def matches(self, device_info):
        # Check for Nest thermostat by MAC prefix
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("18b430") or mac.startswith("641666"):
            return True
        
        # Check for Nest in hostname
        hostname = device_info.get("hostname", "").lower()
        if "nest" in hostname or "thermostat" in hostname:
            return True
            
        # Check for Google Cast (Nest devices often support this)
        mdns_type = device_info.get("mdns_type", "")
        if "_googlecast._tcp.local." in mdns_type:
            return True
            
        return False

    def enrich(self, device_info):
        mac = device_info.get("mac", "").lower().replace(":", "")
        hostname = device_info.get("hostname", "").lower()
        
        if mac.startswith("18b430"):
            return {
                "manufacturer": "Google",
                "category": "Nest Thermostat",
                "device_type": "nest_thermostat"
            }
        elif mac.startswith("641666"):
            return {
                "manufacturer": "Google", 
                "category": "Nest Device",
                "device_type": "nest_device"
            }
        else:
            return {
                "manufacturer": "Google",
                "category": "Nest Device",
                "device_type": "nest_device"
            }

    def get_commands(self):
        return ["control_nest_thermostat"] 