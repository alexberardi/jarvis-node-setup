from core.ijarvis_integration import IJarvisIntegration

class AmazonKindleIntegration(IJarvisIntegration):
    @property
    def name(self):
        return "amazon_kindle"

    @property
    def fingerprints(self):
        return [
            {
                "dhcp": {"hostname": "kindle-"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "dhcp": {"hostname": "kindle"},
                "ssdp": {"manufacturer": "Amazon.com"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "modelName": "Kindle*"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "modelName": "Fire*"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "modelName": "Kindle Paperwhite*"}
            },
            {
                "ssdp": {"manufacturer": "Amazon.com", "modelName": "Kindle Oasis*"}
            }
        ]

    def matches(self, device_info):
        # Check for Kindle specific hostname patterns first (highest priority)
        hostname = device_info.get("hostname", "").lower()
        if "kindle" in hostname:
            return True
            
        # Check for Amazon Kindle in SSDP with specific model names
        ssdp = device_info.get("ssdp", {})
        if "amazon.com" in ssdp.get("manufacturer", "").lower():
            model_name = ssdp.get("modelName", "").lower()
            if "kindle" in model_name or "fire" in model_name:
                return True
            
        return False

    def enrich(self, device_info):
        hostname = device_info.get("hostname", "").lower()
        
        if "kindle" in hostname:
            return {
                "manufacturer": "Amazon",
                "category": "E-Reader",
                "device_type": "amazon_kindle"
            }
        else:
            return {
                "manufacturer": "Amazon",
                "category": "E-Reader",
                "device_type": "amazon_kindle"
            }

    def get_commands(self):
        return ["control_amazon_kindle"] 