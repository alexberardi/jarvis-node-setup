#!/usr/bin/env python3
"""
Authorize a node for development by registering it with command center.

Command center handles the registration with jarvis-auth automatically,
ensuring both systems stay in sync.

Usage:
    python utils/authorize_node.py --node-id my-node --household-id <uuid> --room office
    python utils/authorize_node.py --node-id my-node --create-household "Dev Household" \\
        --email user@example.com --password secret --room bedroom

The script will:
1. (Optional) Create a household via jarvis-auth user API
2. Register the node via command center (which forwards to jarvis-auth)
3. Output the node_key to use in your config
"""

import argparse
import json
import sys
from typing import Any

import httpx


DEFAULT_CC_URL = "http://localhost:8002"
DEFAULT_CC_ADMIN_KEY = "admin_key"
DEFAULT_AUTH_URL = "http://localhost:8007"  # Only used for household creation


def _make_cc_request(
    method: str,
    url: str,
    admin_key: str,
    data: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any] | None]:
    """Make an authenticated request to command center admin API."""
    headers = {"X-API-Key": admin_key}

    with httpx.Client(timeout=10.0) as client:
        if method == "GET":
            resp = client.get(url, headers=headers)
        elif method == "POST":
            resp = client.post(url, headers=headers, json=data)
        elif method == "DELETE":
            resp = client.delete(url, headers=headers)
        elif method == "PATCH":
            resp = client.patch(url, headers=headers, json=data)
        else:
            raise ValueError(f"Unsupported method: {method}")

        try:
            return resp.status_code, resp.json()
        except json.JSONDecodeError:
            return resp.status_code, None


# ============================================================
# Command Center Node Operations
# ============================================================


def list_nodes(cc_url: str, admin_key: str) -> list[dict[str, Any]]:
    """List all registered nodes."""
    status, data = _make_cc_request("GET", f"{cc_url}/api/v0/admin/nodes", admin_key)
    if status != 200:
        print(f"Failed to list nodes: {status}", file=sys.stderr)
        return []
    return data or []


def get_node(cc_url: str, admin_key: str, node_id: str) -> dict[str, Any] | None:
    """Check if node exists."""
    nodes = list_nodes(cc_url, admin_key)
    for node in nodes:
        if node.get("node_id") == node_id:
            return node
    return None


def create_node(
    cc_url: str,
    admin_key: str,
    node_id: str,
    household_id: str,
    room: str,
    name: str | None = None,
    user: str = "default",
    voice_mode: str = "brief",
) -> dict[str, Any] | None:
    """Create a node via command center.

    Command center will register with jarvis-auth and create local record.
    Returns the response including the node_key.
    """
    payload = {
        "node_id": node_id,
        "household_id": household_id,
        "room": room,
        "user": user,
        "voice_mode": voice_mode,
    }
    if name:
        payload["name"] = name

    status, data = _make_cc_request("POST", f"{cc_url}/api/v0/admin/nodes", admin_key, payload)

    if status == 200 or status == 201:
        return data
    if status == 400:
        detail = data.get("detail", "Unknown error") if data else "Unknown error"
        print(f"Failed to create node: {detail}", file=sys.stderr)
        return None
    if status == 404:
        detail = data.get("detail", "Not found") if data else "Not found"
        print(f"Failed to create node: {detail}", file=sys.stderr)
        return None
    if status == 502:
        print("Failed to create node: jarvis-auth service unavailable", file=sys.stderr)
        return None

    print(f"Failed to create node: {status} - {data}", file=sys.stderr)
    return None


def delete_node(cc_url: str, admin_key: str, node_id: str) -> bool:
    """Delete a node."""
    status, data = _make_cc_request("DELETE", f"{cc_url}/api/v0/admin/nodes/{node_id}", admin_key)
    if status == 200:
        return True
    if status == 404:
        print(f"Node '{node_id}' not found", file=sys.stderr)
        return False
    print(f"Failed to delete node: {status} - {data}", file=sys.stderr)
    return False


def update_node(
    cc_url: str,
    admin_key: str,
    node_id: str,
    room: str | None = None,
    user: str | None = None,
    voice_mode: str | None = None,
) -> dict[str, Any] | None:
    """Update a node's local settings."""
    payload = {}
    if room is not None:
        payload["room"] = room
    if user is not None:
        payload["user"] = user
    if voice_mode is not None:
        payload["voice_mode"] = voice_mode

    if not payload:
        print("No fields to update", file=sys.stderr)
        return None

    status, data = _make_cc_request("PATCH", f"{cc_url}/api/v0/admin/nodes/{node_id}", admin_key, payload)
    if status == 200:
        return data
    print(f"Failed to update node: {status} - {data}", file=sys.stderr)
    return None


# ============================================================
# Household Creation (via jarvis-auth user API)
# ============================================================


def create_household_via_user_api(
    auth_url: str,
    email: str,
    password: str,
    household_name: str,
) -> dict[str, Any] | None:
    """Create a household via jarvis-auth user authentication flow.

    This is the only operation that talks directly to jarvis-auth,
    because household creation requires user authentication.
    """
    with httpx.Client(timeout=10.0) as client:
        # Login to get access token
        login_resp = client.post(
            f"{auth_url}/auth/login",
            json={"email": email, "password": password},
        )
        if login_resp.status_code != 200:
            print(f"Login failed: {login_resp.status_code}", file=sys.stderr)
            return None

        token_data = login_resp.json()
        access_token = token_data.get("access_token")

        # Create household
        create_resp = client.post(
            f"{auth_url}/households",
            headers={"Authorization": f"Bearer {access_token}"},
            json={"name": household_name},
        )
        if create_resp.status_code != 201:
            print(f"Failed to create household: {create_resp.status_code}", file=sys.stderr)
            return None

        return create_resp.json()


# ============================================================
# Main
# ============================================================


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Authorize a node by registering with command center",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Register a node with an existing household
  python utils/authorize_node.py --node-id test-node --household-id <uuid> --room office

  # Register and create household (requires user credentials)
  python utils/authorize_node.py --node-id test-node --create-household "Dev Home" \\
      --email user@example.com --password secret --room bedroom

  # List all nodes
  python utils/authorize_node.py --list

  # Delete a node
  python utils/authorize_node.py --node-id test-node --delete

  # Update a node's room
  python utils/authorize_node.py --node-id test-node --update --room "living room"
        """,
    )

    # Service URLs
    parser.add_argument("--cc-url", default=DEFAULT_CC_URL, help="command center base URL")
    parser.add_argument("--cc-key", default=DEFAULT_CC_ADMIN_KEY, help="command center admin key")
    parser.add_argument("--auth-url", default=DEFAULT_AUTH_URL, help="jarvis-auth URL (for household creation)")

    # Actions
    parser.add_argument("--list", action="store_true", help="List all registered nodes")
    parser.add_argument("--delete", action="store_true", help="Delete a node")
    parser.add_argument("--update", action="store_true", help="Update a node's settings")

    # Node identification
    parser.add_argument("--node-id", help="Node ID to register/update/delete")

    # Node creation fields
    parser.add_argument("--name", help="Friendly name for the node (defaults to node-id)")
    parser.add_argument("--household-id", help="Household UUID to register node under")
    parser.add_argument("--room", default="default", help="Room name (default: 'default')")
    parser.add_argument("--user", default="default", help="User name (default: 'default')")
    parser.add_argument("--voice-mode", default="brief", help="Voice mode (default: 'brief')")

    # Household creation (requires user auth)
    parser.add_argument("--create-household", metavar="NAME", help="Create a new household with this name")
    parser.add_argument("--email", help="Email for household creation (user auth)")
    parser.add_argument("--password", help="Password for household creation")

    # Output
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument(
        "--update-config",
        metavar="FILE",
        help="Update the specified config file with new credentials",
    )

    args = parser.parse_args()

    # List nodes
    if args.list:
        nodes = list_nodes(args.cc_url, args.cc_key)
        if args.json:
            print(json.dumps(nodes, indent=2, default=str))
        else:
            if not nodes:
                print("No nodes registered")
            else:
                print(f"{'Node ID':<30} {'Room':<20} {'User':<15} {'Voice Mode':<12}")
                print("-" * 80)
                for node in nodes:
                    print(
                        f"{node['node_id']:<30} "
                        f"{node['room']:<20} "
                        f"{node['user']:<15} "
                        f"{node['voice_mode']:<12}"
                    )
        return 0

    # Require node-id for other actions
    if not args.node_id:
        parser.error("--node-id is required for this action")

    # Delete node
    if args.delete:
        if delete_node(args.cc_url, args.cc_key, args.node_id):
            print(f"Deleted node: {args.node_id}")
            return 0
        return 1

    # Update node
    if args.update:
        result = update_node(
            args.cc_url,
            args.cc_key,
            args.node_id,
            room=args.room if args.room != "default" else None,
            user=args.user if args.user != "default" else None,
            voice_mode=args.voice_mode if args.voice_mode != "brief" else None,
        )
        if result:
            print(f"Updated node: {args.node_id}")
            if args.json:
                print(json.dumps(result, indent=2))
            return 0
        return 1

    # Create household if requested
    household_id = args.household_id
    if args.create_household:
        if not args.email or not args.password:
            parser.error("--email and --password required to create household")

        household = create_household_via_user_api(
            args.auth_url,
            args.email,
            args.password,
            args.create_household,
        )
        if not household:
            return 1

        household_id = household["id"]
        print(f"Created household: {args.create_household} ({household_id})")

    if not household_id:
        parser.error("--household-id or --create-household required")

    # Check if node already exists
    existing = get_node(args.cc_url, args.cc_key, args.node_id)
    if existing:
        print(f"Node '{args.node_id}' already exists in room '{existing['room']}'")
        print("Use --delete to remove it first, or --update to change settings")
        return 1

    # Create node
    result = create_node(
        args.cc_url,
        args.cc_key,
        args.node_id,
        household_id,
        args.room,
        name=args.name,
        user=args.user,
        voice_mode=args.voice_mode,
    )

    if not result:
        return 1

    node_key = result.get("node_key", "")

    if args.json:
        print(json.dumps(result, indent=2, default=str))
    else:
        print(f"\nNode registered successfully!")
        print(f"  node_id: {result['node_id']}")
        print(f"  room: {result['room']}")
        print(f"  user: {result['user']}")
        print(f"  voice_mode: {result['voice_mode']}")
        print(f"\n  node_key: {node_key}")
        print("\nUpdate your config.json with:")
        print(f'  "node_id": "{result["node_id"]}",')
        print(f'  "api_key": "{node_key}"')

    # Update config file if requested
    if args.update_config and node_key:
        try:
            with open(args.update_config, "r") as f:
                config = json.load(f)

            config["node_id"] = result["node_id"]
            config["api_key"] = node_key

            with open(args.update_config, "w") as f:
                json.dump(config, f, indent=2)
                f.write("\n")

            print(f"\nUpdated config file: {args.update_config}")
        except (OSError, json.JSONDecodeError) as e:
            print(f"Failed to update config: {e}", file=sys.stderr)
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
