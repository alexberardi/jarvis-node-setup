# PRD: Declarative Command Authentication Framework — Node-Side

## Status: Ready for Implementation

## Context

The declarative command auth framework allows commands to declare their auth needs via `IJarvisCommand.authentication`. JCC (Command Center) acts as the OAuth redirect authority — it handles code exchange, token encryption, and storage. The node pulls credentials from JCC after authentication completes.

**Already implemented:**
- `AuthenticationConfig` dataclass (`core/ijarvis_authentication.py`) — includes `supports_pkce` field
- `IJarvisCommand` extensions: `authentication`, `needs_auth()`, `store_auth_values()`
- `command_auth` table + `command_auth_service.py`
- HA commands declare auth config + `store_auth_values()` on `ControlDeviceCommand`
- 401 detection in `HomeAssistantService`
- CC: `AuthSession` model + OAuth router (`app/api/oauth.py`)
- CC: Caddy reverse proxy for HTTPS (required for OAuth callbacks)
- Mobile: `IntegrationAuthScreen` (JCC-backed flow), `authSessionApi.ts`

**What remains (this PRD):**
Node-side runtime behavior — pulling credentials from JCC when auth completes.

---

## New Flow (JCC as Redirect Authority)

```
1. Mobile reads settings snapshot → sees needs_auth: true
2. Mobile discovers provider if needed (network scan for HA)
3. Mobile → JCC: POST /oauth/sessions { provider, node_id, provider_base_url, auth_config }
4. JCC creates session (state, PKCE if supported) → returns { session_id, authorize_url }
5. Mobile: openAuthSessionAsync(authorize_url, redirectUrl = jcc_callback_url)
6. User authenticates in browser → Provider redirects to JCC callback
7. JCC: validates state, exchanges code for tokens, stores encrypted, marks session ACTIVE
8. JCC publishes MQTT: jarvis/auth/{provider}/ready
9. Mobile: polls GET /oauth/sessions/{id} → confirms ACTIVE → shows success
10. Node ← MQTT → calls GET /oauth/provider/{provider}/credentials (app-to-app auth)
11. Node calls command.store_auth_values(credentials) → e.g., LLAT creation for HA
```

**Key difference from previous design:** Tokens never touch mobile. JCC handles the code exchange and encrypted token storage. Node pulls credentials over TLS with app-to-app auth.

---

## 1. Settings Snapshot Builder — Auth Section

### What
When the node builds an encrypted settings snapshot for the mobile app, it must include an `integrations` section listing all commands' auth status + configs.

### Schema
```json
{
  "schema_version": 2,
  "commands_schema_version": 1,
  "integrations": [
    {
      "provider": "home_assistant",
      "needs_auth": true,
      "auth_error": null,
      "last_authed_at": null,
      "authentication": {
        "type": "oauth",
        "provider": "home_assistant",
        "client_id": "http://jarvis-node-mobile",
        "keys": ["access_token"],
        "authorize_path": "/auth/authorize",
        "exchange_path": "/auth/token",
        "discovery_port": 8123,
        "discovery_probe_path": "/api/",
        "send_redirect_uri_in_exchange": false,
        "supports_pkce": false
      }
    }
  ]
}
```

### Implementation
- In the snapshot builder (wherever settings are assembled), iterate registered commands
- Group by `provider` (deduplicate — multiple commands may share a provider)
- For each unique provider:
  - Get `AuthenticationConfig` from the first command with that provider
  - Get `CommandAuthStatus` from `command_auth_service.get_auth_status(provider)`
  - If no status row exists, check `needs_auth()` on the command to determine initial state
  - Serialize to the schema above using `AuthenticationConfig.to_dict()`
- Include in the encrypted snapshot alongside existing fields

### Files to modify
- Settings snapshot builder (likely in `services/` or `provisioning/`)

---

## 2. MQTT Handler for Auth Ready

### What
When JCC completes an OAuth exchange, it publishes `jarvis/auth/{provider}/ready`. The node must:
1. Subscribe to `jarvis/auth/+/ready`
2. Parse provider + session_id from payload
3. Call `GET /oauth/provider/{provider}/credentials` with app-to-app auth
4. Receive credentials (access_token, base_url, etc.)
5. Find the command that handles this provider
6. Call `command.store_auth_values(credentials)`

### Auth pull logic
```python
def handle_auth_ready(provider: str, payload: dict) -> None:
    """Handle MQTT auth ready notification from JCC."""
    node_id = payload.get("node_id")
    if node_id != MY_NODE_ID:
        return  # Not for us

    # Pull credentials from JCC (app-to-app auth)
    credentials = pull_credentials_from_jcc(provider, node_id)
    if not credentials:
        logger.error("Failed to pull credentials for %s", provider)
        return

    # Build values dict matching store_auth_values() interface
    values = {}
    if credentials.get("access_token"):
        values["access_token"] = credentials["access_token"]
    if credentials.get("base_url"):
        values["_base_url"] = credentials["base_url"]

    # Find the command that handles this provider
    for command in registered_commands:
        auth = command.authentication
        if auth and auth.provider == provider:
            command.store_auth_values(values)
            break
```

### JCC credential endpoint
```
GET /oauth/provider/{provider}/credentials?node_id={node_id}
Headers: X-Jarvis-App-Id, X-Jarvis-App-Key
Response: { access_token, refresh_token, token_data, base_url }
```

### Files to create/modify
- MQTT subscription handler — subscribe to `jarvis/auth/+/ready`
- HTTP client utility for pulling credentials from JCC
- Need access to the command registry to find the right command

---

## 3. LLAT Creation in `store_auth_values()`

### What
`ControlDeviceCommand.store_auth_values()` is already implemented. It:
1. Receives `access_token` + `_base_url` from JCC (previously from mobile)
2. Connects to HA WebSocket on the same LAN
3. Authenticates with the short-lived token
4. Creates a long-lived access token (LLAT)
5. Stores `HOME_ASSISTANT_REST_URL`, `HOME_ASSISTANT_WS_URL`, `HOME_ASSISTANT_API_KEY`
6. Clears the re-auth flag

**This is already done.** The `_create_long_lived_token()` method handles the WebSocket interaction. The interface is the same regardless of how credentials arrive (from K2 config push or from JCC credential pull).

### Dependencies
- `websocket-client` package (add to `requirements.txt` / `JarvisPackage` if not present)

---

## 4. 401 Detection

### What
Already implemented in `HomeAssistantService`. Both `call_service()` and `get_state()` detect HTTP 401 responses and call `_flag_reauth("401 Unauthorized")`, which sets `needs_auth=1` in the `command_auth` table.

**This is already done.**

---

## 5. Legacy K2 Config Push (Deprecated)

The K2-based config push flow (mobile encrypts → CC relays → node decrypts) is still available for non-auth config pushes (device lists, etc.). For auth flows, the JCC-backed OAuth flow (Section 2) is the replacement.

---

## Implementation Order

1. **MQTT subscription for `jarvis/auth/+/ready`** — core runtime behavior
2. **HTTP client for credential pull from JCC** — called by MQTT handler
3. **Settings snapshot builder** — enables the mobile app to see integration status
4. **Integration testing** — end-to-end: mobile OAuth → JCC exchange → MQTT → node LLAT creation

## Testing

### Unit tests
- `command_auth_service`: `set_needs_auth()`, `clear_auth_flag()`, `get_auth_status()`, `get_all_auth_statuses()`
- `AuthenticationConfig.to_dict()`: serialization round-trip, includes `supports_pkce`
- `needs_auth()`: returns True when secrets missing, returns True when re-auth flagged
- `store_auth_values()`: mock WebSocket, verify secrets stored

### Integration tests
- MQTT `jarvis/auth/home_assistant/ready` → pulls credentials from JCC → triggers `store_auth_values()`
- 401 from HA → `needs_auth` flagged → shows in settings snapshot → mobile re-auths → JCC exchanges → node pulls → secrets refreshed

### End-to-end
1. Mobile discovers HA on LAN
2. Mobile creates auth session on JCC
3. User authenticates in WebView → JCC callback → code exchange
4. JCC stores encrypted tokens, publishes MQTT
5. Node receives MQTT, pulls credentials from JCC
6. Node calls `ControlDeviceCommand.store_auth_values()`
7. LLAT created via WebSocket
8. Secrets stored, re-auth flag cleared
9. Voice commands work: "turn on the office lights"
