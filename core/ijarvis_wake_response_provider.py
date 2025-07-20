from abc import ABC, abstractmethod
from typing import Optional


class IJarvisWakeResponseProvider(ABC):
    """Interface for wake response providers that generate dynamic wake responses"""
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return the name of this provider"""
        pass
    
    @abstractmethod
    def fetch_next_wake_response(self) -> Optional[str]:
        """
        Fetch the next wake response text
        
        Returns:
            The wake response text, or None if no response could be fetched
        """
        pass 