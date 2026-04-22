"""Wake response provider that uses command-center's wake-response endpoint.

Migration history:
- Pre-Phase-6: node called jarvis-tts directly for dynamic greetings.
- Phase 6:    node → CC proxy → jarvis-tts. CC handled app-to-app auth
              but jarvis-tts still did the LLM call + any sanitation.
- Current:    node → CC /wake-response. CC owns the LLM call, runs the
              active prompt provider's sanitize_text (strips Qwen3
              <think> blocks, etc.), returns clean text. TTS and
              llm-proxy stay dumb — single source of truth for voice
              response generation.
"""

from typing import Any, Dict, Optional

from clients.rest_client import RestClient
from core.ijarvis_wake_response_provider import IJarvisWakeResponseProvider
from jarvis_log_client import JarvisLogger
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


class JarvisTTSWakeResponseProvider(IJarvisWakeResponseProvider):
    """Wake response provider that uses command-center to generate dynamic responses."""

    @property
    def provider_name(self) -> str:
        return "jarvis-tts-api"

    def fetch_next_wake_response(self) -> Optional[str]:
        """Fetch the next wake response from TTS service via command-center.

        Returns:
            The generated wake response text, or None if failed
        """
        try:
            command_center_url = get_command_center_url()
            if not command_center_url:
                logger.error("command_center_url not configured", context={"provider": "wake-response"})
                return None

            url = f"{command_center_url}/api/v0/wake-response"

            response: Optional[Dict[str, Any]] = RestClient.post(url, timeout=10)

            if response and isinstance(response, dict):
                text = response.get("text", "").strip()
                if text:
                    logger.info(f"Generated wake response: {text}")
                    return text

            logger.warning("No text received from wake response API")
            return None

        except Exception as e:
            logger.error(f"Failed to fetch next greeting: {e}")
            return None
