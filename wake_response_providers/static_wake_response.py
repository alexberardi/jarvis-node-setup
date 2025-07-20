from typing import Optional
from core.ijarvis_wake_response_provider import IJarvisWakeResponseProvider


class StaticWakeResponseProvider(IJarvisWakeResponseProvider):
    """Wake response provider that uses static responses (no dynamic generation)"""
    
    @property
    def provider_name(self) -> str:
        return "static"
    
    def fetch_next_wake_response(self) -> Optional[str]:
        """
        Static provider doesn't generate new responses
        
        Returns:
            None (no dynamic response generation)
        """
        return None 