"""
Service discovery configuration for jarvis-node-setup.

Fetches service URLs from jarvis-config-service if available,
with fallback to JSON config file values.
"""

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

_initialized = False

# Service name in config service -> JSON config key mapping
_SERVICE_TO_CONFIG_KEY = {
    "jarvis-command-center": "jarvis_command_center_api_url",
    "jarvis-auth": "jarvis_auth_api_url",
    "jarvis-whisper": "jarvis_whisper_api_url",
    "jarvis-tts": "jarvis_tts_api_url",
}

# Default URLs if nothing else works
_DEFAULTS = {
    "jarvis-command-center": "http://localhost:8002",
    "jarvis-auth": "http://localhost:8007",
    "jarvis-whisper": "http://localhost:9999",
    "jarvis-tts": "http://localhost:8009",
}

# TODO: jarvis-llm-proxy is currently accessed directly by some commands
# (story_command.py, sync_date_keys.py). This should be refactored to go
# through jarvis-command-center instead. See jarvis_llm_proxy_api_url in
# config files.


def init() -> bool:
    """
    Initialize service discovery from jarvis-config-service.

    Returns True if successful, False if falling back to JSON config.
    """
    global _initialized

    config_url = os.getenv("JARVIS_CONFIG_URL")
    if not config_url:
        logger.debug("JARVIS_CONFIG_URL not set - using JSON config for service URLs")
        return False

    try:
        from jarvis_config_client import init as init_config_client

        success = init_config_client(config_url=config_url)
        if success:
            _initialized = True
            logger.info("Service discovery initialized from %s", config_url)
            return True
        else:
            logger.warning("Config service unavailable - using JSON config")
            return False

    except ImportError:
        logger.debug("jarvis-config-client not installed - using JSON config")
        return False
    except Exception as e:
        logger.error("Failed to initialize service discovery: %s", e)
        return False


def is_initialized() -> bool:
    """Check if service discovery is initialized."""
    return _initialized


def _get_from_json_config(config_key: str) -> Optional[str]:
    """Get URL from JSON config file."""
    try:
        from utils.config_service import Config
        return Config.get_str(config_key)
    except Exception:
        pass  # Config service not available, try next

    try:
        from utils.config_loader import Config
        return Config.get(config_key)
    except Exception:
        pass  # Config loader not available

    return None


def _get_url(service_name: str) -> str:
    """Get URL for a service, with fallback chain."""
    # Try config client first
    if _initialized:
        try:
            from jarvis_config_client import get_service_url
            url = get_service_url(service_name)
            if url:
                return url
        except Exception:
            pass  # Config client failed, fall back to JSON config

    # Fall back to JSON config
    config_key = _SERVICE_TO_CONFIG_KEY.get(service_name)
    if config_key:
        url = _get_from_json_config(config_key)
        if url:
            return url

    # Fall back to default
    return _DEFAULTS.get(service_name, "")


def get_command_center_url() -> str:
    """Get jarvis-command-center service URL."""
    return _get_url("jarvis-command-center")


def get_whisper_url() -> str:
    """Get jarvis-whisper service URL."""
    return _get_url("jarvis-whisper")


def get_tts_url() -> str:
    """Get jarvis-tts service URL."""
    return _get_url("jarvis-tts")


def get_auth_url() -> str:
    """Get jarvis-auth service URL."""
    return _get_url("jarvis-auth")
