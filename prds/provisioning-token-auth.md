# Provisioning Token Auth

## Problem

Node registration currently requires the command-center admin API key:

```
Mobile App → Node (via AP mode) → POST /api/v1/provision {admin_key: "..."}
                                     │
                                     ▼
                              Node → Command Center (POST /api/v0/admin/nodes, X-API-Key: admin_key)
                                     │
                                     ▼
                              Command Center → jarvis-auth (app-to-app, registers node)
```

The admin key is a server-side secret that protects all admin endpoints (node CRUD, settings, etc.). Sending it to the mobile app and then to the node is a security violation — any compromised node or intercepted provisioning request leaks full admin access.

`authorize_node.py` has the same problem: it takes `--cc-key` (the admin key) as a CLI argument.

## Solution: Short-Lived Provisioning Tokens

Replace the admin key with a short-lived, single-use provisioning token scoped to registering one specific node. Command center generates the node's identity (a UUID) at token creation time — no chicken-and-egg problem.

### New Flow

```
1. Mobile app authenticates user (JWT via jarvis-auth)
          │
          ▼
2. Mobile app calls command-center (while on home WiFi, behind the scenes):
   POST /api/v0/provisioning/token
   Authorization: Bearer <user_jwt>
   {household_id: "uuid", room: "kitchen"}
          │
          ▼
3. Command center generates a UUID (the node's permanent identity) + provisioning token
   Returns: {token: "prov_xxxx", node_id: "uuid-guid", expires_in: 600}
          │
          ▼
4. Mobile app connects to node AP, sends K2 + provision request:
   POST /api/v1/provision
   {wifi_ssid, wifi_password, room, command_center_url,
    household_id, node_id: "uuid-guid", provisioning_token: "prov_xxxx"}
          │
          ▼
5. Node stores CC-assigned node_id as its identity (keeps local name for AP/display)
          │
          ▼
6. Node connects to WiFi, then calls command-center:
   POST /api/v0/nodes/register
   {node_id: "uuid-guid", provisioning_token: "prov_xxxx"}
          │
          ▼
7. Command center validates token, registers with jarvis-auth, returns node_key
   Returns: {node_id, node_key, room}
          │
          ▼
8. Node stores node_key locally, provisioning complete
```

### Key Design Decisions

- **CC generates the node identity.** The mobile app doesn't need to know the node_id before requesting a token. No "Enter Node ID" screen needed.
- **Token fetch is invisible to the user.** The mobile app requests a token behind the scenes while still on home WiFi, before connecting to the node AP. No new screens.
- **Node keeps its local name.** The node retains its self-generated local name (e.g., from MAC address) for AP SSID and local display, but adopts the CC-generated UUID as its permanent identity for all CC communication.
- **Auto-refresh.** If a token expires mid-flow, the mobile app can request a new token for the same UUID by passing `node_id` back to the token endpoint.

### What Changes

| Component | Change |
|-----------|--------|
| **command-center** | New `POST /api/v0/provisioning/token` endpoint (JWT or admin key auth, generates UUID) |
| **command-center** | New `POST /api/v0/nodes/register` endpoint (token auth, no admin key) |
| **command-center** | Token storage + validation (DB with bcrypt hashing) |
| **jarvis-node-setup** | `provisioning/models.py` adds `node_id` + `provisioning_token`, removes `admin_key` |
| **jarvis-node-setup** | `provisioning/api.py` stores CC-assigned `node_id`, passes token to registration |
| **jarvis-node-setup** | `provisioning/registration.py` calls new register endpoint with token |
| **jarvis-node-setup** | `authorize_node.py` uses token flow for dev registration (CC generates node_id) |
| **jarvis-node-mobile** | Fetches token + UUID behind the scenes before connecting to node AP |

### What Doesn't Change

- jarvis-auth internal API (command-center still calls `/internal/nodes/register` with app-to-app auth)
- Node authentication after provisioning (`X-API-Key: node_id:node_key`)
- Admin endpoints still use admin key (for server-side admin tools)
- Node's local name / AP SSID (still derived from MAC or local config)

---

## Command Center: New Endpoints

See `jarvis-command-center/prds/provisioning-token-auth.md` for full implementation details. Summary below.

### `POST /api/v0/provisioning/token`

**Auth:** Accepts either:
- User JWT: `Authorization: Bearer <token>` (mobile app flow)
- Admin key: `X-API-Key: <admin_key>` (dev CLI flow via `authorize_node.py`)

**Request:**
```json
{
  "household_id": "uuid-of-household",
  "room": "kitchen",
  "name": "Kitchen Speaker",
  "node_id": "existing-guid"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `household_id` | Yes | UUID of the household |
| `room` | No | Room location (defaults to `"default"`) |
| `name` | No | Friendly name for the node |
| `node_id` | No | If provided, refreshes token for an existing UUID. If omitted, generates a new UUID. |

**Response (201):**
```json
{
  "token": "prov_<url-safe-base64>",
  "node_id": "550e8400-e29b-41d4-a716-446655440000",
  "expires_at": "2026-02-10T15:10:00Z",
  "expires_in": 600
}
```

**Token properties:**
- Prefixed with `prov_` for easy identification
- 24+ bytes of randomness (URL-safe base64)
- Expires in 10 minutes (configurable)
- Single-use: consumed on successful registration
- Scoped to a specific `node_id` — can't be used to register a different node
- Stored hashed in the database (bcrypt)

### `POST /api/v0/nodes/register`

**Auth:** Provisioning token in request body (no admin key, no JWT).

**Request:**
```json
{
  "node_id": "550e8400-e29b-41d4-a716-446655440000",
  "provisioning_token": "prov_<url-safe-base64>",
  "room": "kitchen"
}
```

| Field | Required | Description |
|-------|----------|-------------|
| `node_id` | Yes | The UUID the node received during provisioning |
| `provisioning_token` | Yes | The token to authenticate the registration |
| `room` | No | Room override (uses token's room if omitted) |

**Response (201):**
```json
{
  "node_id": "550e8400-e29b-41d4-a716-446655440000",
  "node_key": "eyJhbGciOiJIUzI1NiIs...",
  "room": "kitchen"
}
```

**Error responses:**

| Status | Condition | Body |
|--------|-----------|------|
| 401 | Token invalid, expired, or already consumed | `{"detail": "Invalid or expired provisioning token"}` |
| 401 | `node_id` doesn't match token scope | `{"detail": "Invalid or expired provisioning token"}` |
| 400 | Node already registered | `{"detail": "Node already exists"}` |
| 502 | jarvis-auth unreachable | `{"detail": "Auth service unavailable"}` |

---

## Node Setup: Changes

### `provisioning/models.py`

Replace `admin_key` with `node_id` + `provisioning_token`:

```python
class ProvisionRequest(BaseModel):
    wifi_ssid: str
    wifi_password: str
    room: str
    command_center_url: str
    household_id: str
    node_id: str              # CC-assigned UUID (new)
    provisioning_token: str   # Was: admin_key: Optional[str]
```

The `node_id` is the CC-generated UUID that the mobile app received from the token endpoint and passed to the node during provisioning.

### `provisioning/api.py`

Update `_run_provisioning` to:
1. Store the CC-assigned `node_id` from the provision request as the node's identity
2. Pass `provisioning_token` and `node_id` to the registration step
3. Keep the node's local name (from MAC/config) for AP SSID and display purposes

```python
# In _run_provisioning, after receiving ProvisionRequest:
# 1. Store CC-assigned node_id
config.set("node_id", request.node_id)

# 2. Connect to WiFi (existing)
# ...

# 3. Register with CC using the token (include room for CC to store)
result = register_with_command_center(
    command_center_url=request.command_center_url,
    node_id=request.node_id,          # CC-assigned UUID
    provisioning_token=request.provisioning_token,
    room=request.room,
)
```

### `provisioning/registration.py`

Replace admin-key registration with token-based:

```python
def register_with_command_center(
    command_center_url: str,
    node_id: str,
    provisioning_token: str,
    room: str | None = None,
) -> dict | None:
    """Register node using a provisioning token."""
    url = f"{command_center_url.rstrip('/')}/api/v0/nodes/register"

    payload: dict[str, str] = {
        "node_id": node_id,
        "provisioning_token": provisioning_token,
    }
    if room:
        payload["room"] = room

    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.post(
                url,
                json=payload,
            )

            if response.status_code in (200, 201):
                data = response.json()
                return {
                    "node_id": data.get("node_id"),
                    "node_key": data.get("node_key"),
                }

            return None
    except httpx.RequestError:
        return None
```

### `utils/authorize_node.py`

Update to use the provisioning token flow. **CC generates the node_id** — the script no longer needs `--node-id` for creation:

1. Call `POST /api/v0/provisioning/token` with admin key (`X-API-Key` header) — CC generates a UUID
2. Call `POST /api/v0/nodes/register` with the provisioning token + UUID

```python
def main() -> int:
    init_service_discovery()
    # ... argparse setup ...
    # --node-id is NO LONGER REQUIRED for creation (CC generates it)
    # --node-id is still used for --list, --delete, --update

    # Step 1: Get provisioning token (CC generates the UUID)
    token_response = create_provisioning_token(
        cc_url=args.cc_url,
        admin_key=args.cc_key,
        household_id=household_id,
        room=args.room,
        name=args.name,
    )
    if not token_response:
        return 1

    node_id = token_response["node_id"]   # CC-generated UUID
    token = token_response["token"]

    print(f"  Generated node_id: {node_id}")

    # Step 2: Register using token
    result = register_with_token(
        cc_url=args.cc_url,
        node_id=node_id,
        provisioning_token=token,
    )
    # ... handle result, output node_id + node_key ...
```

The admin key is only used to create the token (step 1). The `POST /api/v0/admin/nodes` endpoint is no longer used for registration — it can be kept for other admin CRUD (list, update, delete).

### Delete legacy registration

Remove `register_with_command_center_legacy()` from `provisioning/registration.py` — it's already marked deprecated.

---

## Mobile App: Changes

See `jarvis-node-mobile/prds/provisioning-token-auth.md` for full implementation details. Summary below.

The mobile app provisioning flow changes from:

```
Old: POST /api/v1/provision {admin_key: "secret"}
New: POST /api/v1/provision {node_id: "uuid-guid", provisioning_token: "prov_xxxx", ...}
```

The mobile app fetches the token **behind the scenes** while still on home WiFi:

```
POST <command_center_url>/api/v0/provisioning/token
Authorization: Bearer <user_jwt>
{household_id: "uuid", room: "kitchen"}
→ Returns: {token: "prov_xxxx", node_id: "uuid-guid"}
```

No new screens needed. The token + UUID are stored in memory and sent to the node during provisioning.

### Mobile App Flow

```
1. User opens app, authenticates (has JWT)
2. User taps "Add Node"
3. App fetches provisioning token from CC (behind the scenes, on home WiFi)
   → receives token + node_id (CC-generated UUID)
4. App connects to node AP
5. Scan networks → user picks WiFi + enters password + room
6. Send K2 to node
7. Send provision request to node:
   {wifi_ssid, wifi_password, room, command_center_url,
    household_id, node_id, provisioning_token}
8. User switches to home WiFi → polls status → done
```

Only ONE WiFi switch (home → node AP → home). No "Enter Node ID" screen.

---

## Security Properties

| Property | Old (admin key) | New (provisioning token) |
|----------|----------------|------------------------|
| Scope | Full admin access | Single node registration |
| Lifetime | Permanent | 10 minutes |
| Reusability | Unlimited | Single-use |
| Revocability | Change env var, restart | Auto-expires |
| Exposure | Mobile app + node + network | Mobile app + node (briefly) |
| Compromise impact | All admin operations | Register one extra node |

---

## TDD Test Plan

### Command Center Tests

See `jarvis-command-center/prds/provisioning-token-auth.md` for the full 19-test plan. Key tests:

```
1. test_create_token_with_admin_key — 201, token + UUID returned
2. test_create_token_with_jwt — 201, valid UUID
3. test_create_token_no_auth — 401
4. test_create_token_generates_unique_guids — two tokens have different UUIDs
5. test_register_with_valid_token — 201, node_key returned, node in DB
6. test_register_consumes_token — second use returns 401
7. test_register_expired_token — 401
8. test_register_wrong_node_id — 401
9. test_register_node_already_exists — 400
10. test_refresh_token_same_guid — same UUID, new token, old invalidated
11. test_register_with_room_override — request room beats token room
12. test_cleanup_expired_tokens — expired removed from DB
```

### Node Setup Tests

```
1. test_provision_request_model_has_token_and_node_id
   - Assert ProvisionRequest has provisioning_token field
   - Assert ProvisionRequest has node_id field
   - Assert admin_key field is removed

2. test_provision_stores_cc_node_id
   - Mock provisioning flow
   - Assert CC-assigned node_id is stored as the node's identity

3. test_register_with_token_success
   - Mock command-center /nodes/register response
   - Assert node_key saved to config
   - Assert node_id in request matches CC-assigned UUID

4. test_register_with_token_failure
   - Mock 401 response
   - Assert provisioning continues (non-fatal, matches current behavior)

5. test_node_keeps_local_name
   - Provision with CC-assigned node_id
   - Assert local name (from MAC/config) is preserved
   - Assert CC node_id is stored separately
```

---

## Implementation Order

### Phase 1: Command Center
1. Alembic migration for `provisioning_tokens` table
2. Pydantic models (request/response schemas)
3. Auth dependency (`verify_provisioning_auth` — dual JWT + admin key)
4. Token creation endpoint with UUID generation + refresh support
5. Token validation logic (`_validate_provisioning_token`)
6. Node registration endpoint (`POST /api/v0/nodes/register`)
7. Token cleanup (startup sweep + optional periodic)
8. Router registration in `app/main.py`

### Phase 2: Node Setup
1. Update `provisioning/models.py` — add `node_id`, replace `admin_key` with `provisioning_token`
2. Update `provisioning/api.py` — store CC-assigned `node_id`, pass token through
3. Update `provisioning/registration.py` — call new register endpoint with token
4. Delete `register_with_command_center_legacy()`
5. Update `authorize_node.py` — two-step flow: get token (CC generates UUID), register with token

### Phase 3: Mobile App
1. Fetch provisioning token behind the scenes on "Add Node"
2. Store token + CC-generated `node_id` in memory
3. Send both to node in provision request

---

## Out of Scope

- **Rate limiting on token creation** — could add later if abuse is a concern
- **QR code provisioning** — alternative to AP-mode dance, separate feature
- **Revoking active tokens** — they expire in 10 minutes, not worth the complexity
