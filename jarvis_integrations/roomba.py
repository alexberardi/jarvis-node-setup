from core.ijarvis_integration import IJarvisIntegration

class RoombaIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "roomba"

    @property
    def fingerprints(self):
        return [
            # MAC address patterns (verified from Home Assistant)
            {
                "dhcp": {"macaddress": "50:14:79"},
                "ssdp": {"manufacturer": "iRobot"}
            },
            {
                "dhcp": {"macaddress": "50:14:79"},
                "zeroconf": {"type": "_irobot._tcp.local."}
            },
            {
                "dhcp": {"macaddress": "00:12:37"},
                "ssdp": {"manufacturer": "iRobot"}
            },
            {
                "dhcp": {"macaddress": "00:12:37"},
                "zeroconf": {"type": "_irobot._tcp.local."}
            },
            # Hostname patterns
            {
                "dhcp": {"hostname": "roomba-"},
                "ssdp": {"manufacturer": "iRobot"}
            },
            {
                "dhcp": {"hostname": "irobot-"},
                "ssdp": {"manufacturer": "iRobot"}
            },
            {
                "dhcp": {"hostname": "roomba"},
                "ssdp": {"manufacturer": "iRobot"}
            },
            # Fallback patterns
            {
                "ssdp": {"manufacturer": "iRobot"}
            },
            {
                "zeroconf": {"type": "_irobot._tcp.local."}
            }
        ]

    def matches(self, device_info):
        # Check for Roomba by MAC prefix (verified)
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("501479") or mac.startswith("001237"):
            return True
        
        # Check for Roomba in hostname
        hostname = device_info.get("hostname", "").lower()
        if "roomba" in hostname or "irobot" in hostname:
            return True
            
        # Check for iRobot in SSDP
        ssdp = device_info.get("ssdp", {})
        if "irobot" in ssdp.get("manufacturer", "").lower():
            return True
            
        # Check for iRobot service
        mdns_type = device_info.get("mdns_type", "")
        if "_irobot._tcp.local." in mdns_type:
            return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "roomba" in hostname:
            return {
                "manufacturer": "iRobot",
                "category": "Robot Vacuum",
                "device_type": "roomba"
            }
        else:
            return {
                "manufacturer": "iRobot",
                "category": "Robot Vacuum",
                "device_type": "roomba"
            }

    def get_commands(self):
        return ["control_roomba"] 