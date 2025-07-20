from core.ijarvis_integration import IJarvisIntegration

class ESPHomeIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "esphome"

    @property
    def fingerprints(self):
        return [
            # Home Assistant verified patterns
            {
                "zeroconf": {"type": "_esphome._tcp.local."}
            },
            # MAC address patterns (verified from Home Assistant)
            {
                "dhcp": {"macaddress": "24:6F:28"},
                "zeroconf": {"type": "_esphome._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "24:0A:C4"},
                "zeroconf": {"type": "_esphome._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "18:FE:34"},
                "zeroconf": {"type": "_esphome._tcp.local."}
            },
            # Hostname patterns
            {
                "dhcp": {"hostname": "esphome-"},
                "zeroconf": {"type": "_esphome._tcp.local."}
            },
            {
                "dhcp": {"hostname": "esp32-"},
                "zeroconf": {"type": "_esphome._tcp.local."}
            },
            {
                "dhcp": {"hostname": "esp8266-"},
                "zeroconf": {"type": "_esphome._tcp.local."}
            },
            # Fallback patterns
            {
                "dhcp": {"macaddress": "24:6F:28"}
            },
            {
                "dhcp": {"macaddress": "24:0A:C4"}
            },
            {
                "dhcp": {"macaddress": "18:FE:34"}
            }
        ]

    def matches(self, device_info):
        # Check for ESPHome by MAC prefix (verified)
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("246f28") or mac.startswith("240ac4") or mac.startswith("18fe34"):
            return True
        
        # Check for ESPHome in hostname
        hostname = device_info.get("hostname", "").lower()
        if "esphome" in hostname or "esp32" in hostname or "esp8266" in hostname:
            return True
            
        # Check for ESPHome service (Home Assistant verified)
        mdns_type = device_info.get("mdns_type", "")
        if "_esphome._tcp.local." in mdns_type:
            return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "esphome" in hostname:
            return {
                "manufacturer": "ESPHome",
                "category": "IoT Device",
                "device_type": "esphome"
            }
        elif "esp32" in hostname:
            return {
                "manufacturer": "Espressif",
                "category": "IoT Device",
                "device_type": "esphome"
            }
        else:
            return {
                "manufacturer": "ESPHome",
                "category": "IoT Device",
                "device_type": "esphome"
            }

    def get_commands(self):
        return ["control_esphome"] 