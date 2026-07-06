"""
Startup detection logic for provisioning.

Determines if the node is provisioned and can reach the command center.
"""

import os
from pathlib import Path
from typing import Optional

import httpx
from jarvis_log_client import JarvisLogger

from utils.encryption_utils import get_secret_dir

logger = JarvisLogger(service="jarvis-node")


def _get_provisioned_marker() -> Path:
    """Get the path to the .provisioned marker file."""
    secret_dir = get_secret_dir()
    return secret_dir / ".provisioned"


def _get_command_center_url() -> Optional[str]:
    """
    Get the command center URL from config.

    Checks environment variable first, then config.json.
    """
    # Check environment variable
    url = os.environ.get("COMMAND_CENTER_URL")
    if url:
        return url

    # Try to load from config.json
    config_path = os.environ.get("CONFIG_PATH")
    if config_path:
        try:
            import json
            with open(config_path) as f:
                config = json.load(f)
                return config.get("jarvis_command_center_api_url")
        except (FileNotFoundError, json.JSONDecodeError, KeyError):
            pass

    return None


def _can_reach_command_center(url: str) -> bool:
    """
    Check if the command center is reachable.

    Args:
        url: Command center base URL

    Returns:
        True if health endpoint responds, False otherwise
    """
    try:
        health_url = f"{url.rstrip('/')}/health"
        with httpx.Client(timeout=5.0) as client:
            response = client.get(health_url)
            return response.status_code == 200
    except httpx.RequestError:
        return False


# Exponential backoff schedule for CC connectivity checks at boot.
# Total wait: ~85s — long enough for slow network init, short enough
# that a relocated node enters AP mode within ~2 minutes.
_RETRY_DELAYS: list[float] = [2, 2, 3, 3, 5, 5, 5, 10, 10, 10, 15, 15]


def has_provisioning_marker() -> bool:
    """Check if the .provisioned marker file exists (no network check)."""
    return _get_provisioned_marker().exists()


def is_provisioned() -> bool:
    """Whether this node has completed provisioning. MARKER-ONLY.

    Deliberately consults nothing but the ``.provisioned`` marker.
    The previous implementation also required command-center
    reachability, which stranded a provisioned node in AP mode whenever
    CC was unreachable for ~85s at boot (2026-07-05 prod-kitchen: WiFi
    slow to associate after a net-watchdog reboot → AP mode → NetworkManager
    stopped → node off-network until a physical power-cycle; the AP-mode
    recovery watcher cannot save it because the AP's captive DNS points
    its reachability probe back at the node itself). Reachability is a
    runtime concern — see ``wait_for_command_center``.
    """
    return has_provisioning_marker()


def should_enter_provisioning() -> bool:
    """The AP-mode gate: enter provisioning ONLY when the marker is absent.

    Entering AP mode tears down the WiFi client, so a wrong "yes" is
    unrecoverable without physical access. A provisioned node that cannot
    reach the network keeps running (and stays SSH-able the moment WiFi
    returns); re-provisioning a relocated node is an explicit user action
    (factory reset via the app clears the marker), never an automatic
    fallback.
    """
    return not has_provisioning_marker()


def _has_lan_connectivity() -> bool:
    """True when a default route exists and its gateway answers a ping."""
    import re
    import subprocess

    try:
        out = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    m = re.search(r"default via (\S+)", out)
    if not m:
        return False
    try:
        return subprocess.run(
            ["ping", "-c", "1", "-W", "2", m.group(1)],
            capture_output=True, timeout=5,
        ).returncode == 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False


# WiFi-join grace at boot for provisioned nodes. Long enough for a slow
# association (the 2026-07-05 kitchen boot needed more than the old 85s
# CC window); short enough that a genuinely changed WiFi reaches the
# recoverable AP within a few minutes.
_WIFI_JOIN_POLL_SECONDS: float = 5.0
_WIFI_JOIN_ATTEMPTS: int = 36  # ~3 minutes


def wait_for_wifi() -> bool:
    """Wait for LAN connectivity after boot (provisioned nodes only).

    Distinguishes the two outage shapes that used to be conflated:
    WiFi itself not joining (→ caller may enter the RECOVERABLE AP mode,
    where the AP↔STA cycle keeps retrying the known network) versus WiFi
    up but command-center unreachable (→ never AP mode; see
    ``wait_for_command_center``).
    """
    import time

    attempts = int(os.environ.get("JARVIS_WIFI_JOIN_ATTEMPTS", _WIFI_JOIN_ATTEMPTS))
    for attempt in range(attempts):
        if _has_lan_connectivity():
            return True
        if attempt == 0 or (attempt + 1) % 6 == 0:
            logger.info("Waiting for WiFi/LAN", attempt=attempt + 1, max_attempts=attempts)
        time.sleep(_WIFI_JOIN_POLL_SECONDS)
    logger.warning("No LAN connectivity after boot grace",
                   total_wait_seconds=attempts * _WIFI_JOIN_POLL_SECONDS)
    return False


def wait_for_command_center() -> bool:
    """Boot-ordering grace wait for command center. Informational only.

    Gives CC ~85s (exponential backoff) to become reachable so startup
    proceeds in a sensible order. The result must NEVER gate provisioning
    state — callers log and continue on False; MQTT/heartbeat/voice all
    retry at runtime.

    Returns:
        True when CC answered within the window, False otherwise.
    """
    import time

    url = _get_command_center_url()
    if not url:
        logger.warning("No command center URL configured — skipping boot grace wait")
        return False

    max_attempts: int = len(_RETRY_DELAYS)
    for attempt, delay in enumerate(_RETRY_DELAYS):
        if _can_reach_command_center(url):
            return True
        logger.info("Waiting for command center",
                    attempt=attempt + 1, max_attempts=max_attempts,
                    retry_in_seconds=delay)
        time.sleep(delay)

    logger.warning("Could not reach command center after retries",
                   total_wait_seconds=sum(_RETRY_DELAYS))
    return False


def mark_provisioned() -> None:
    """Create the .provisioned marker file."""
    marker = _get_provisioned_marker()
    marker.parent.mkdir(mode=0o700, exist_ok=True)
    marker.touch()
    os.chmod(marker, 0o600)


def clear_provisioned() -> None:
    """Remove the .provisioned marker file for re-provisioning."""
    marker = _get_provisioned_marker()
    if marker.exists():
        marker.unlink()
