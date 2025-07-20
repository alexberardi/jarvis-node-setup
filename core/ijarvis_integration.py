from abc import ABC, abstractmethod
from typing import List, Dict

class IJarvisIntegration(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def fingerprints(self) -> List[Dict]: ...

    @abstractmethod
    def matches(self, device_info: Dict) -> bool: ...

    @abstractmethod
    def enrich(self, device_info: Dict) -> Dict: ...

    @abstractmethod
    def get_commands(self) -> List: ... 