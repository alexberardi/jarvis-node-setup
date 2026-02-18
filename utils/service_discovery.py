"""
Service discovery configuration for jarvis-node-setup.

Fetches service URLs from jarvis-config-service if available,
with fallback to JSON config file values.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)

_initialized = False

# Service name (short) -> JSON config key mapping
_SERVICE_TO_CONFIG_KEY = {
    "command-center": "jarvis_command_center_api_url",
    "auth": "jarvis_auth_api_url",
    "whisper": "jarvis_whisper_api_url",
    "tts": "jarvis_tts_api_url",
}

# Default URLs if nothing else works
_DEFAULTS = {
    "jarvis-command-center": "http://localhost:7703",
    "jarvis-auth": "http://localhost:7701",
    "jarvis-whisper": "http://localhost:7706",
    "jarvis-tts": "http://localhost:7707",
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

    try:
        from jarvis_config_client import init as init_config_client

        success = init_config_client()
        if success:
            _initialized = True
            logger.info("Service discovery initialized")
            return True
        else:
            logger.warning("Config service unavailable - using JSON config")
            return False

    except ImportError:
        logger.debug("jarvis-config-client not installed - using JSON config")
        return False
    except (OSError, RuntimeError) as e:
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
    except (ImportError, AttributeError):
        pass

    try:
        from utils.config_loader import Config
        return Config.get(config_key)
    except (ImportError, AttributeError):
        pass

    return None


def _get_url(service_name: str) -> str:
    """Get URL for a service, with fallback to JSON config."""
    if _initialized:
        try:
            from jarvis_config_client import get_service_url
            url = get_service_url(service_name)
            if url:
                return url
        except (ImportError, RuntimeError):
            pass

    # Fall back to JSON config
    config_key = _SERVICE_TO_CONFIG_KEY.get(service_name)
    if config_key:
        url = _get_from_json_config(config_key)
        if url:
            return url

    return ""


def get_command_center_url() -> str:
    """Get command-center service URL."""
    return _get_url("command-center")


def get_whisper_url() -> str:
    """Get whisper service URL."""
    return _get_url("whisper")


def get_tts_url() -> str:
    """Get tts service URL."""
    return _get_url("tts")


def get_auth_url() -> str:
    """Get auth service URL."""
    return _get_url("auth")
