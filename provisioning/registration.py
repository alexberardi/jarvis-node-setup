"""
Command center registration for newly provisioned nodes.

Uses provisioning tokens (short-lived, single-use) instead of admin API keys.
The command center generates the node UUID at token creation time, and the
mobile app passes both the UUID and token to the node during provisioning.
"""

import time

import httpx
from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

# Registration runs seconds after the WiFi join, and `nmcli connection up`
# returns on association — DHCP/routes/DNS can settle a few seconds later.
# A connect-level failure in that window is transient, so retry over ~30s
# before declaring the provisioning attempt failed.
_CONNECT_RETRIES = 8
_CONNECT_RETRY_DELAY_S = 4.0


def register_with_command_center(
    command_center_url: str,
    node_id: str,
    provisioning_token: str,
    room: str | None = None,
) -> dict | None:
    """
    Register this node with the command center using a provisioning token.

    Args:
        command_center_url: Base URL of the command center (e.g., http://192.168.1.50:7703)
        node_id: CC-assigned UUID for this node
        provisioning_token: Short-lived provisioning token from command center
        room: Room name for this node (optional)

    Returns:
        Dict with node_id and node_key on success, None on failure
    """
    url = f"{command_center_url.rstrip('/')}/api/v0/nodes/register"
    payload: dict = {
        "node_id": node_id,
        "provisioning_token": provisioning_token,
    }
    if room is not None:
        payload["room"] = room

    logger.info("Registering with command center", url=url, node_id=node_id)

    for attempt in range(1, _CONNECT_RETRIES + 1):
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, json=payload)

            if response.status_code in (200, 201):
                data = response.json()
                logger.info("Registration successful", node_id=data.get("node_id"))
                return {
                    "node_id": data.get("node_id"),
                    "node_key": data.get("node_key"),
                }

            # An HTTP response means the network is fine and CC rejected us
            # (bad/expired token, duplicate node, ...) — retrying can't help.
            logger.error(
                "Registration failed",
                status=response.status_code,
                body=response.text[:500],
            )
            return None

        except httpx.RequestError as e:
            if attempt < _CONNECT_RETRIES:
                logger.warning(
                    "Registration request failed, retrying",
                    error=str(e),
                    attempt=attempt,
                    retries_left=_CONNECT_RETRIES - attempt,
                )
                time.sleep(_CONNECT_RETRY_DELAY_S)
            else:
                logger.error("Registration request failed", error=str(e))

    return None
