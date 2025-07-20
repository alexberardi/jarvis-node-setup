from core.ijarvis_integration import IJarvisIntegration

class LGTvIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "lg_tv"

    @property
    def fingerprints(self):
        return [
            {
                "dhcp": {"macaddress": "A0:AB:1B"},
                "ssdp": {"manufacturer": "LG Electronics"}
            },
            {
                "dhcp": {"macaddress": "A0:AB:1B"},
                "zeroconf": {"type": "_webostv._tcp.local."}
            },
            {
                "ssdp": {"manufacturer": "LG Electronics", "deviceType": "urn:schemas-upnp-org:device:MediaRenderer:1"}
            }
        ]

    def matches(self, device_info):
        # Check for LG TV by MAC prefix
        mac = device_info.get("mac", "").lower().replace(":", "")
        if mac.startswith("a0ab1b"):
            return True
        
        # Check for LG in hostname
        hostname = device_info.get("hostname", "").lower()
        if "lg" in hostname or "webos" in hostname:
            return True
            
        # Check for LG in SSDP
        ssdp = device_info.get("ssdp", {})
        if "lg electronics" in ssdp.get("manufacturer", "").lower():
            return True
            
        # Check for WebOS service
        mdns_type = device_info.get("mdns_type", "")
        if "_webostv._tcp.local." in mdns_type:
            return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "webos" in hostname:
            return {
                "manufacturer": "LG Electronics",
                "category": "Smart TV",
                "device_type": "lg_tv",
                "model": "WebOS TV"
            }
        else:
            return {
                "manufacturer": "LG Electronics",
                "category": "Smart TV",
                "device_type": "lg_tv"
            }

    def get_commands(self):
        return ["control_lg_tv"] 