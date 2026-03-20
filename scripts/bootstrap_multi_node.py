#!/usr/bin/env python3
"""Bootstrap multi-node test environment.

Creates 2 households, 3 users, and 6 nodes for testing multi-user,
multi-household, and multi-node scenarios.

Prerequisites:
  - jarvis-auth (7701) and jarvis-command-center (7703) must be running
  - ADMIN_API_KEY from jarvis-command-center/.env

Usage:
    python scripts/bootstrap_multi_node.py --cc-key <ADMIN_API_KEY>

    # Teardown (deletes all created nodes)
    python scripts/bootstrap_multi_node.py --cc-key <ADMIN_API_KEY> --teardown

Layout:
    Household A ("Home")       Household B ("Cabin")
    ├── User: alice             ├── User: bob
    ├── User: charlie (member)  ├── User: charlie (member, shared)
    ├── node-kitchen  (7771)    ├── node-living  (7774)
    ├── node-bedroom  (7772)    ├── node-garage  (7775)
    └── node-office   (7773)    └── node-deck    (7776)
"""

import argparse
import json
import sys
from pathlib import Path

import httpx

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
CONFIGS_DIR = REPO_ROOT / "configs"

AUTH_URL = "http://localhost:7701"
CC_URL = "http://localhost:7703"

# Use host.docker.internal so containers can reach host-mapped services
# regardless of which Docker network the target is on.
CC_INTERNAL_URL = "http://host.docker.internal:7703"
MQTT_HOST = "host.docker.internal"
CONFIG_SERVICE_URL = "http://host.docker.internal:7700"

USERS = [
    {"email": "alice@test.jarvis", "username": "alice", "password": "TestPass123!"},
    {"email": "bob@test.jarvis", "username": "bob", "password": "TestPass123!"},
    {"email": "charlie@test.jarvis", "username": "charlie", "password": "TestPass123!"},
]

HOUSEHOLDS = [
    {"name": "Home", "owner": "alice"},
    {"name": "Cabin", "owner": "bob"},
]

# node-name → (household, room, host-port)
NODES = {
    "node-kitchen": ("Home", "kitchen", 7771),
    "node-bedroom": ("Home", "bedroom", 7772),
    "node-office": ("Home", "office", 7773),
    "node-living": ("Cabin", "living room", 7774),
    "node-garage": ("Cabin", "garage", 7775),
    "node-deck": ("Cabin", "deck", 7776),
}


def register_user(email: str, username: str, password: str) -> dict | None:
    """Register a user via jarvis-auth. Returns token data or None."""
    try:
        resp = httpx.post(
            f"{AUTH_URL}/auth/register",
            json={"email": email, "username": username, "password": password},
            timeout=10.0,
        )
        if resp.status_code == 201:
            return resp.json()
        if resp.status_code in (400, 409):
            # Already exists — try login
            return login_user(email, password)
        print(f"  Failed to register {username}: {resp.status_code} {resp.text}")
        return None
    except httpx.RequestError as e:
        print(f"  Network error registering {username}: {e}")
        return None


def login_user(email: str, password: str) -> dict | None:
    """Login via jarvis-auth. Returns token data."""
    try:
        resp = httpx.post(
            f"{AUTH_URL}/auth/login",
            json={"email": email, "password": password},
            timeout=10.0,
        )
        if resp.status_code == 200:
            return resp.json()
        print(f"  Login failed for {email}: {resp.status_code}")
        return None
    except httpx.RequestError as e:
        print(f"  Network error logging in {email}: {e}")
        return None


def create_household(access_token: str, name: str) -> str | None:
    """Create a household, or return existing one with the same name."""
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        # Check if household already exists
        list_resp = httpx.get(f"{AUTH_URL}/households", headers=headers, timeout=10.0)
        if list_resp.status_code == 200:
            for h in list_resp.json():
                if h["name"] == name:
                    return h["id"]

        # Create new
        resp = httpx.post(
            f"{AUTH_URL}/households",
            json={"name": name},
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code == 201:
            return resp.json()["id"]
        print(f"  Failed to create household '{name}': {resp.status_code} {resp.text}")
        return None
    except httpx.RequestError as e:
        print(f"  Network error creating household: {e}")
        return None


def add_member_to_household(
    access_token: str, household_id: str, user_id: int, role: str = "MEMBER"
) -> bool:
    """Add a user to a household."""
    try:
        resp = httpx.post(
            f"{AUTH_URL}/households/{household_id}/members",
            json={"user_id": user_id, "role": role},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        return resp.status_code in (200, 201)
    except httpx.RequestError:
        return False


def register_node(
    cc_url: str, admin_key: str, household_id: str, room: str, name: str
) -> dict | None:
    """Register a node via CC provisioning token flow."""
    headers = {"X-API-Key": admin_key}

    # Step 1: Create provisioning token
    try:
        resp = httpx.post(
            f"{cc_url}/api/v0/provisioning/token",
            json={"household_id": household_id, "room": room, "name": name},
            headers=headers,
            timeout=10.0,
        )
        if resp.status_code not in (200, 201):
            print(f"  Provisioning token failed for {name}: {resp.status_code} {resp.text}")
            return None

        token_data = resp.json()
        node_id = token_data["node_id"]
        prov_token = token_data["token"]
    except httpx.RequestError as e:
        print(f"  Network error creating token for {name}: {e}")
        return None

    # Step 2: Register with token
    try:
        resp = httpx.post(
            f"{cc_url}/api/v0/nodes/register",
            json={"node_id": node_id, "provisioning_token": prov_token},
            timeout=10.0,
        )
        if resp.status_code not in (200, 201):
            print(f"  Registration failed for {name}: {resp.status_code} {resp.text}")
            return None

        result = resp.json()
        result["household_id"] = household_id
        return result
    except httpx.RequestError as e:
        print(f"  Network error registering {name}: {e}")
        return None


def delete_node(cc_url: str, access_token: str, node_id: str) -> bool:
    """Delete a node via CC admin API (JWT auth)."""
    try:
        resp = httpx.delete(
            f"{cc_url}/api/v0/admin/nodes/{node_id}",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10.0,
        )
        return resp.status_code == 200
    except httpx.RequestError:
        return False


def write_node_config(name: str, node_id: str, api_key: str, room: str, household_id: str) -> None:
    """Write a node's Docker config file."""
    CONFIGS_DIR.mkdir(exist_ok=True)
    config = {
        "node_id": node_id,
        "api_key": api_key,
        "room": room,
        "voice_mode": "text",
        "household_id": household_id,
        "jarvis_command_center_api_url": CC_INTERNAL_URL,
        "mqtt_broker": str(MQTT_HOST),
        "mqtt_port": 1884,
        "mqtt_enabled": True,
        "jarvis_config_service_url": CONFIG_SERVICE_URL,
    }
    config_path = CONFIGS_DIR / f"{name}.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)
        f.write("\n")


def bootstrap(admin_key: str) -> int:
    """Create users, households, and nodes."""
    print("=== Multi-Node Bootstrap ===\n")

    # 1. Register users
    print("1. Registering users...")
    user_tokens: dict[str, dict] = {}  # username → {access_token, user_id, ...}
    for user in USERS:
        result = register_user(user["email"], user["username"], user["password"])
        if not result:
            print(f"   FAILED: {user['username']}")
            return 1
        user_tokens[user["username"]] = {
            "access_token": result["access_token"],
            "user_id": result.get("user", {}).get("id") or result.get("user_id"),
        }
        print(f"   OK: {user['username']} (user_id={user_tokens[user['username']]['user_id']})")

    # 2. Create households
    print("\n2. Creating households...")
    household_ids: dict[str, str] = {}  # name → id
    for hh in HOUSEHOLDS:
        owner = hh["owner"]
        token = user_tokens[owner]["access_token"]

        # Owner's default household is "My Home" — create the named one
        hh_id = create_household(token, hh["name"])
        if not hh_id:
            print(f"   FAILED: {hh['name']}")
            return 1
        household_ids[hh["name"]] = hh_id
        print(f"   OK: {hh['name']} ({hh_id[:8]}...) owner={owner}")

    # 3. Add charlie to both households
    print("\n3. Adding shared member (charlie) to both households...")
    charlie_id = user_tokens["charlie"]["user_id"]
    for hh_name, hh_id in household_ids.items():
        # Find the household owner's token
        owner = next(h["owner"] for h in HOUSEHOLDS if h["name"] == hh_name)
        token = user_tokens[owner]["access_token"]
        if add_member_to_household(token, hh_id, charlie_id, "MEMBER"):
            print(f"   OK: charlie added to {hh_name}")
        else:
            print(f"   WARN: could not add charlie to {hh_name} (may already be member)")

    # 4. Register nodes
    print("\n4. Registering nodes...")
    node_results: dict[str, dict] = {}
    for node_name, (hh_name, room, port) in NODES.items():
        hh_id = household_ids[hh_name]
        result = register_node(CC_URL, admin_key, hh_id, room, node_name)
        if not result:
            print(f"   FAILED: {node_name}")
            return 1
        node_results[node_name] = result
        print(f"   OK: {node_name} → {hh_name}/{room} (port {port}, id={result['node_id'][:8]}...)")

    # 5. Write config files
    print("\n5. Writing config files...")
    for node_name, (hh_name, room, port) in NODES.items():
        result = node_results[node_name]
        write_node_config(
            name=node_name,
            node_id=result["node_id"],
            api_key=result["node_key"],
            room=room,
            household_id=result["household_id"],
        )
        print(f"   OK: configs/{node_name}.json")

    # 6. Summary
    print("\n=== Bootstrap Complete ===\n")
    print("Households:")
    for name, hid in household_ids.items():
        print(f"  {name}: {hid}")
    print(f"\nUsers: {', '.join(u['username'] for u in USERS)}")
    print(f"Nodes: {len(node_results)}")
    print(f"\nConfig files written to: {CONFIGS_DIR}/")
    print("\nNext steps:")
    print("  docker compose -f docker-compose.multi-node.yaml up -d")
    print("  python scripts/setup_docker_nodes.py   # install commands + generate K2s")
    print("  # Import K2s into mobile app (Nodes → Import Key → Paste from Clipboard)")

    # Save state for teardown
    # Map each node to its household so we can login as the owner to delete
    node_households: dict[str, str] = {}
    for node_name, (hh_name, _, _) in NODES.items():
        node_households[node_name] = hh_name

    household_owners: dict[str, dict] = {}
    for hh in HOUSEHOLDS:
        user = next(u for u in USERS if u["username"] == hh["owner"])
        household_owners[hh["name"]] = {"email": user["email"], "password": user["password"]}

    state = {
        "households": household_ids,
        "nodes": {name: r["node_id"] for name, r in node_results.items()},
        "node_households": node_households,
        "household_owners": household_owners,
        "users": {u["username"]: user_tokens[u["username"]]["user_id"] for u in USERS},
    }
    state_path = CONFIGS_DIR / ".bootstrap-state.json"
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)

    return 0


def teardown(admin_key: str) -> int:
    """Delete all nodes created by bootstrap."""
    state_path = CONFIGS_DIR / ".bootstrap-state.json"
    if not state_path.exists():
        print("No bootstrap state found. Run bootstrap first.")
        return 1

    with open(state_path) as f:
        state = json.load(f)

    print("=== Multi-Node Teardown ===\n")

    # Login as each household owner to get JWTs for deletion
    household_owners = state.get("household_owners", {})
    node_households = state.get("node_households", {})
    owner_tokens: dict[str, str] = {}  # household_name → access_token

    for hh_name, creds in household_owners.items():
        result = login_user(creds["email"], creds["password"])
        if result:
            owner_tokens[hh_name] = result["access_token"]
            print(f"  Logged in as {creds['email']} ({hh_name})")
        else:
            print(f"  WARN: could not login as {creds['email']} — nodes in {hh_name} won't be deleted")

    # Delete nodes using the owner's JWT
    for name, node_id in state.get("nodes", {}).items():
        hh_name = node_households.get(name, "")
        token = owner_tokens.get(hh_name)
        if not token:
            print(f"  SKIP: {name} — no token for {hh_name}")
            continue
        if delete_node(CC_URL, token, node_id):
            print(f"  Deleted node: {name} ({node_id[:8]}...)")
        else:
            print(f"  WARN: could not delete {name}")

    # Remove config files
    for name in state.get("nodes", {}).keys():
        config_path = CONFIGS_DIR / f"{name}.json"
        if config_path.exists():
            config_path.unlink()
            print(f"  Removed: configs/{name}.json")

    state_path.unlink()
    print("\nTeardown complete.")
    return 0


def main() -> int:
    global AUTH_URL, CC_URL

    parser = argparse.ArgumentParser(description="Bootstrap multi-node test environment")
    parser.add_argument("--cc-key", required=True, help="Command center ADMIN_API_KEY")
    parser.add_argument("--cc-url", default=CC_URL, help="Command center URL")
    parser.add_argument("--auth-url", default=AUTH_URL, help="Auth service URL")
    parser.add_argument("--teardown", action="store_true", help="Delete all created nodes")
    args = parser.parse_args()

    AUTH_URL = args.auth_url
    CC_URL = args.cc_url

    if args.teardown:
        return teardown(args.cc_key)
    return bootstrap(args.cc_key)


if __name__ == "__main__":
    sys.exit(main())
