"""
Command center registration for newly provisioned nodes.
"""

import httpx


def register_with_command_center(
    command_center_url: str,
    node_id: str,
    room: str,
    api_key: str
) -> bool:
    """
    Register this node with the command center.

    Args:
        command_center_url: Base URL of the command center (e.g., http://192.168.1.50:8002)
        node_id: Unique identifier for this node
        room: Room name for this node
        api_key: API key for authentication

    Returns:
        True if registration successful, False otherwise
    """
    try:
        url = f"{command_center_url.rstrip('/')}/api/v0/admin/nodes"

        payload = {
            "node_id": node_id,
            "room": room,
            "capabilities": ["voice", "speaker"],
            "status": "online"
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": api_key
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)

            if response.status_code in (200, 201):
                return True

            # If node already exists, that's okay
            if response.status_code == 409:
                return True

            return False

    except httpx.RequestError:
        return False
