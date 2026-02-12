from abc import ABC, abstractmethod
from typing import Dict

class IJarvisIntegration(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def matches(self, device_info: Dict) -> bool: ...

    @abstractmethod
    def enrich(self, device_info: Dict) -> Dict: ...

    def confidence(self, device_info: Dict) -> float:
        """
        Default confidence scoring.
        Subclasses can override for better logic.
        """
        score = 0.0
        vendor = device_info.get("vendor", "").lower()
        mdns = device_info.get("mdns_services", [])
        ssdp = device_info.get("ssdp_services", [])

        # ✅ Base scoring logic
        if self.name.lower() in vendor.lower():
            score += 0.6
        if mdns:
            for svc in mdns:
                if self.name in svc["name"].lower():
                    score += 0.3
        if ssdp:
            for svc in ssdp:
                if self.name in str(svc).lower():
                    score += 0.3

        return min(score, 1.0)

