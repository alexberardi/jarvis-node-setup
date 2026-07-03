"""Fetch the shared MQTT broker credential from command-center.

When broker auth is enabled (the transition→lockdown rollout), a node needs the
shared credential to authenticate to the broker. It fetches that over its
already-authenticated HTTP channel (``X-API-Key``) from command-center's
``/api/v0/node/mqtt-credentials`` — never over MQTT itself, which would be
circular. The creds are persisted to ``config.json`` so the node can reconnect
even if command-center is unreachable later (e.g. after the broker locks down).

Transition-safe: if command-center has no credential yet (returns nulls) or is
unreachable, this returns ``(None, None)`` and the caller connects anonymously,
exactly as today.
"""
import json
import os
from typing import Optional, Tuple

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


def _config_path() -> str:
    return os.path.expandvars(os.path.expanduser(
        os.environ.get("CONFIG_PATH", "config.json")
    ))


def _persist_mqtt_credentials(username: str, password: str) -> bool:
    """Write ``mqtt_username``/``mqtt_password`` to config.json (read-modify-write).

    ``Config.get_str`` re-reads the file on every call, so the persisted values
    are visible to subsequent reads (and survive reboot) with no cache to bust.
    """
    path = _config_path()
    try:
        try:
            with open(path) as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            config = {}
        config["mqtt_username"] = username
        config["mqtt_password"] = password
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except OSError as e:
        logger.warning("persist mqtt credentials failed", error=str(e))
        return False


def fetch_and_persist_mqtt_credentials() -> Tuple[Optional[str], Optional[str]]:
    """Fetch the shared MQTT credential from command-center and persist it.

    Returns ``(username, password)`` on success, or ``(None, None)`` if
    command-center has no credential yet, is unreachable, or returns an unusable
    response. On success the creds are written to ``config.json`` so subsequent
    connects (and reboots) use them even if command-center is later down.
    """
    base_url = get_command_center_url() or ""
    if not base_url:
        logger.warning("MQTT cred fetch skipped: no command-center URL")
        return None, None

    url = f"{base_url.rstrip('/')}/api/v0/node/mqtt-credentials"
    creds = RestClient.get(url, timeout=10)
    if not creds:
        # command-center unreachable or non-2xx — RestClient already logged it.
        return None, None

    username = creds.get("username")
    password = creds.get("password")
    if not username or not password:
        # Broker auth not enabled yet — connect anonymously (transition window).
        logger.info("MQTT broker auth not enabled yet; connecting anonymously")
        return None, None

    if _persist_mqtt_credentials(username, password):
        logger.info("Fetched + persisted MQTT credentials from command-center")
    return username, password
