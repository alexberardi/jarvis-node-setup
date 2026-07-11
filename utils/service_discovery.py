"""
Service discovery for jarvis-node-setup.

Single source of truth: **jarvis-config-service**. The node bootstraps from ONE
address it owns — ``jarvis_config_service_url`` in config.json (a stable IP; if
the operator changes it, that's on them) — and resolves EVERY other service
(command-center, mqtt-broker, whisper, tts, auth) live from config-service via
one uniform code path.

Design decisions (deliberate):
- **No localhost/JSON fallback.** A node that fabricates ``mqtt://localhost:1884``
  when config-service is unreachable looks healthy while reaching nothing —
  that's the recurring "dark node" failure. We refuse to fabricate: a service
  that can't be resolved raises ``ServiceUnresolvedError`` (loud), so callers
  retry/surface instead of silently connecting to nothing.
- **Provisioning is the one exception.** A node that isn't provisioned yet isn't
  expected to reach config-service, so we return "" there instead of raising.
- **Vantage is the config-client's job.** The node declares its style via
  ``JARVIS_CONFIG_URL_STYLE`` (``external`` for an off-box Pi, ``dockerized`` for
  an in-Docker node) — see entrypoint.py. This module just calls
  ``get_service_url`` and trusts the client to apply the right rewrite.
"""

import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_initialized = False


class ServiceUnresolvedError(RuntimeError):
    """A service URL could not be resolved from config-service.

    Deliberately loud. Callers MUST NOT paper over this with a localhost
    default — retry with backoff or surface a discovery-degraded state.
    """


def _in_provisioning_mode() -> bool:
    """True when the node hasn't been provisioned yet (no ``.provisioned``
    marker) or provisioning checks are explicitly skipped. In that state we
    tolerate unresolved services instead of raising."""
    if os.environ.get("JARVIS_SKIP_PROVISIONING_CHECK", "").lower() in ("1", "true", "yes"):
        return True
    secret_dir = os.path.expandvars(os.path.expanduser(
        os.environ.get("JARVIS_SECRET_DIRECTORY", str(Path.home() / ".jarvis"))
    ))
    return not Path(secret_dir, ".provisioned").exists()


def init() -> bool:
    """Initialize service discovery from jarvis-config-service.

    Reads the bootstrap address (``jarvis_config_service_url``) from config.json
    into ``JARVIS_CONFIG_URL`` if not already set. Returns True on success.
    """
    global _initialized

    if not os.environ.get("JARVIS_CONFIG_URL"):
        config_url = _get_from_json_config("jarvis_config_service_url")
        if config_url:
            os.environ["JARVIS_CONFIG_URL"] = config_url

    try:
        from jarvis_config_client import init as init_config_client

        success = bool(init_config_client())
        _initialized = success
        if success:
            logger.info("Service discovery initialized")
        else:
            logger.warning("config-service unavailable at init — will retry on resolve")
        return success
    except ImportError:
        logger.error("jarvis-config-client not installed — cannot discover services")
        return False
    except (OSError, RuntimeError) as e:
        logger.error("Failed to initialize service discovery: %s", e)
        return False


def is_initialized() -> bool:
    """Check if service discovery is initialized."""
    return _initialized


def _get_from_json_config(config_key: str) -> Optional[str]:
    """Read a key from the JSON config file. Used ONLY for the bootstrap
    address (jarvis_config_service_url) — never as a service-URL fallback."""
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
    """Resolve a service URL from config-service.

    Raises ``ServiceUnresolvedError`` if config-service can't resolve it — no
    localhost fabrication — unless the node is still in provisioning mode.
    """
    if _initialized:
        try:
            from jarvis_config_client import get_service_url
            url = get_service_url(service_name)
            if url:
                return url
            logger.warning("config-service returned no URL for %r", service_name)
        except (ImportError, RuntimeError) as e:
            logger.warning("config-service resolve failed for %r: %s", service_name, e)
    else:
        # Not initialized yet — try a one-shot (re)init so a node that booted
        # before config-service self-heals on the next resolve.
        if init():
            try:
                from jarvis_config_client import get_service_url
                url = get_service_url(service_name)
                if url:
                    return url
            except (ImportError, RuntimeError):
                pass

    if _in_provisioning_mode():
        # Not provisioned yet — not expected to reach config-service.
        return ""

    raise ServiceUnresolvedError(
        f"Could not resolve {service_name!r} from config-service "
        f"(JARVIS_CONFIG_URL={os.environ.get('JARVIS_CONFIG_URL')!r}). "
        "config-service must be reachable — refusing to fabricate a localhost URL."
    )


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


def get_mqtt_broker_url() -> str:
    """Get MQTT broker URL — same uniform path as every other service.

    Supports mqtt/mqtts/ws/wss (external nodes use wss:// via Cloudflare Tunnel;
    LAN nodes use mqtt://). No localhost fallback: raises if unresolved so the
    MQTT layer retries the real broker instead of connecting to nothing.
    """
    return _get_url("jarvis-mqtt-broker")
