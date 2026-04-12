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
    """
    Check if the node is provisioned and can reach the command center.

    Logic:
    1. Check if ~/.jarvis/.provisioned marker exists
       - No → return False (needs provisioning)
    2. Try to ping command center health endpoint with exponential backoff
       - Success → return True (ready for normal operation)
       - Fail after all retries → return False (network changed, needs re-provisioning)

    Returns:
        True if provisioned and command center reachable, False otherwise.
    """
    import time

    # Step 1: Check marker file
    if not has_provisioning_marker():
        return False

    # Step 2: Check command center connectivity with exponential backoff
    # Network may take time to come up after boot
    url = _get_command_center_url()
    if not url:
        # No URL configured, consider not provisioned
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
