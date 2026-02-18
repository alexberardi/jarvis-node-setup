"""
Command center registration for newly provisioned nodes.

Uses provisioning tokens (short-lived, single-use) instead of admin API keys.
The command center generates the node UUID at token creation time, and the
mobile app passes both the UUID and token to the node during provisioning.
"""

import httpx


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
    try:
        url = f"{command_center_url.rstrip('/')}/api/v0/nodes/register"

        payload: dict = {
            "node_id": node_id,
            "provisioning_token": provisioning_token,
        }
        if room is not None:
            payload["room"] = room

        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload)

            if response.status_code in (200, 201):
                data = response.json()
                return {
                    "node_id": data.get("node_id"),
                    "node_key": data.get("node_key"),
                }

            return None

    except httpx.RequestError:
        return None
