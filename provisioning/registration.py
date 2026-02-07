"""
Command center registration for newly provisioned nodes.

TODO: Update to use new auth flow
=====================================
The current implementation uses a legacy format that doesn't integrate with
jarvis-auth. The new flow should be:

1. Mobile app provisions node with:
   - WiFi credentials
   - command_center_url
   - household_id (from user's authenticated session)
   - user_id (optional, who is registering)

2. Node calls command center's /api/v0/admin/nodes with:
   - node_id
   - household_id
   - room
   - name (optional)

3. Command center forwards to jarvis-auth, which:
   - Creates the node registration
   - Generates node_key
   - Grants service access

4. Command center returns node_key to node

5. Node stores node_id and node_key in local config

See: jarvis-command-center/app/admin.py for the new endpoint format
See: jarvis-node-setup/utils/authorize_node.py for CLI registration tool
"""

import httpx


def register_with_command_center(
    command_center_url: str,
    node_id: str,
    room: str,
    household_id: str,
    name: str | None = None,
    admin_key: str | None = None,
) -> dict | None:
    """
    Register this node with the command center.

    This creates records in both command center (local context) and
    jarvis-auth (credentials/authorization).

    Args:
        command_center_url: Base URL of the command center (e.g., http://192.168.1.50:8002)
        node_id: Unique identifier for this node
        room: Room name for this node
        household_id: UUID of the household this node belongs to
        name: Friendly name for the node (defaults to node_id)
        admin_key: Admin API key for command center (required for registration)

    Returns:
        Dict with node_id and node_key on success, None on failure
    """
    if not admin_key:
        # TODO: In production, this should use a different auth mechanism
        # Perhaps the mobile app provides a short-lived registration token
        return None

    try:
        url = f"{command_center_url.rstrip('/')}/api/v0/admin/nodes"

        payload = {
            "node_id": node_id,
            "household_id": household_id,
            "room": room,
            "name": name or node_id,
        }

        headers = {
            "Content-Type": "application/json",
            "X-API-Key": admin_key,
        }

        with httpx.Client(timeout=30.0) as client:
            response = client.post(url, json=payload, headers=headers)

            if response.status_code in (200, 201):
                data = response.json()
                return {
                    "node_id": data.get("node_id"),
                    "node_key": data.get("node_key"),
                }

            # If node already exists, that's an error now (can't get key back)
            return None

    except httpx.RequestError:
        return None


# Legacy function for backwards compatibility
def register_with_command_center_legacy(
    command_center_url: str,
    node_id: str,
    room: str,
    api_key: str
) -> bool:
    """
    DEPRECATED: Use register_with_command_center instead.

    This legacy function doesn't integrate with jarvis-auth and will
    fail with the new auth flow.
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
