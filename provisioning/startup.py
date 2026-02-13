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
        health_url = f"{url.rstrip('/')}/api/v0/health"
        with httpx.Client(timeout=5.0) as client:
            response = client.get(health_url)
            return response.status_code == 200
    except httpx.RequestError:
        return False


def is_provisioned(max_retries: int = 10, retry_delay: float = 3.0) -> bool:
    """
    Check if the node is provisioned and can reach the command center.

    Logic:
    1. Check if ~/.jarvis/.provisioned marker exists
       - No → return False (needs provisioning)
    2. Try to ping command center health endpoint with retries
       - Success → return True (ready for normal operation)
       - Fail after all retries → return False (network changed, needs re-provisioning)

    Args:
        max_retries: Number of attempts to reach command center (default 10)
        retry_delay: Seconds between retries (default 3.0)

    Returns:
        True if provisioned and command center reachable, False otherwise.
    """
    import time

    marker = _get_provisioned_marker()

    # Step 1: Check marker file
    if not marker.exists():
        return False

    # Step 2: Check command center connectivity with retries
    # Network may take time to come up after boot
    url = _get_command_center_url()
    if not url:
        # No URL configured, consider not provisioned
        return False

    for attempt in range(max_retries):
        if _can_reach_command_center(url):
            return True
        if attempt < max_retries - 1:
            logger.info(f"Waiting for network... attempt {attempt + 1}/{max_retries}")
            time.sleep(retry_delay)

    logger.warning("Could not reach command center after retries")
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
