from typing import Optional

import httpx

from core.ijarvis_wake_response_provider import IJarvisWakeResponseProvider
from utils.config_service import Config


class JarvisTTSWakeResponseProvider(IJarvisWakeResponseProvider):
    """Wake response provider that uses jarvis-tts-api to generate dynamic responses"""
    
    @property
    def provider_name(self) -> str:
        return "jarvis-tts-api"
    
    def fetch_next_wake_response(self) -> Optional[str]:
        """
        Fetch the next wake response from jarvis-tts-api
        
        Returns:
            The generated wake response text, or None if failed
        """
        try:
            base_url = Config.get_str("jarvis_tts_api_url")
            if not base_url:
                print("[wake-response] jarvis_tts_api_url not configured")
                return None
                
            # Use the same pattern as JarvisTTS - concatenate the endpoint
            url = base_url + "/generate-wake-response"
                
            response = httpx.post(url, timeout=10.0)
            response.raise_for_status()

            text = response.json().get("text", "").strip()
            
            if text:
                print(f"[wake-response] Generated: {text}")
                return text
            else:
                print("[wake-response] No text received from API")
                return None

        except Exception as e:
            print(f"[wake-response] Failed to fetch next greeting: {e}")
            return None 