#!/bin/bash
# compose/seed.sh — runs in CI after the auth + config-service containers
# are healthy but before CC is started. Registers the app-clients and
# service URLs CC needs to talk to its dependencies, and exports captured
# secrets to $GITHUB_ENV so subsequent workflow steps see them.
#
# Run locally (against a `docker compose up -d postgres jarvis-auth
# jarvis-config-service mosquitto` stack):
#
#     AUTH_URL=http://localhost:7701 \
#     CONFIG_URL=http://localhost:7700 \
#     GITHUB_ENV=/tmp/seed.env \
#         bash compose/seed.sh

set -euo pipefail

AUTH_URL="${AUTH_URL:-http://localhost:7701}"
CONFIG_URL="${CONFIG_URL:-http://localhost:7700}"
AUTH_ADMIN_TOKEN="${AUTH_ADMIN_TOKEN:-ci-auth-admin-token}"
CONFIG_ADMIN_TOKEN="${CONFIG_ADMIN_TOKEN:-ci-auth-admin-token}"

# Send all logging to stderr so it doesn't pollute the stdout capture
# inside `$(register_app_client ...)`. The previous version sent these
# to stdout and the captured string ended up "[seed] ...\n<json>", which
# python's json.load couldn't parse.
log() { echo "[seed] $*" >&2; }

# POST to an admin endpoint. Writes the response body to /tmp/seed_resp.json,
# echoes the body to stdout on 2xx, and on non-2xx prints HTTP status +
# request body + response body to stderr and returns non-zero so the CI
# log makes it obvious what the service actually said. (The previous
# `curl -sf` swallowed all of that and we ended up debugging a useless
# `JSONDecodeError`.)
#
# Usage: http_post <url> <header_name> <header_value> <body>
http_post() {
  local url="$1"
  local header_name="$2"
  local header_value="$3"
  local body="$4"
  local resp_file=/tmp/seed_resp.json
  local status
  local -a curl_args=(-sS -o "$resp_file" -w "%{http_code}" -X POST "$url"
                       -H "Content-Type: application/json" -d "$body")
  # Empty header_name → no auth header (used for POST /auth/register, which
  # is the only endpoint here that takes no admin token).
  if [[ -n "$header_name" ]]; then
    curl_args+=(-H "$header_name: $header_value")
  fi
  status=$(curl "${curl_args[@]}")
  if [[ "$status" -lt 200 || "$status" -ge 300 ]]; then
    log "FAIL: POST $url → HTTP $status"
    log "Request body: $body"
    log "Response body:"
    cat "$resp_file" >&2
    echo >&2
    return 1
  fi
  cat "$resp_file"
}

register_app_client() {
  local app_id="$1"
  local name="$2"
  log "Registering app-client: $app_id"
  http_post "$AUTH_URL/admin/app-clients" \
    "X-Jarvis-Admin-Token" "$AUTH_ADMIN_TOKEN" \
    "{\"app_id\":\"$app_id\",\"name\":\"$name\"}"
}

register_service() {
  local name="$1"
  local host="$2"
  local port="$3"
  log "Registering service: $name → $host:$port"
  # config-service uses a different admin-token header name than auth does
  # (X-Admin-Token vs X-Jarvis-Admin-Token).
  http_post "$CONFIG_URL/services" \
    "X-Admin-Token" "$CONFIG_ADMIN_TOKEN" \
    "{\"name\":\"$name\",\"host\":\"$host\",\"port\":$port,\"scheme\":\"http\",\"health_path\":\"/health\"}"
}

# POST /auth/setup — auth's first-superuser endpoint. Same body + response
# shape as /auth/register, but the created user has is_superuser=true.
# Switched from /auth/register in v2.16 so CASE-214 (factory-reset) can
# pass — that endpoint requires either node.household_id + power_user
# role OR is_superuser, and CC's create_node doesn't currently persist
# household_id on the local Node row, so the only path through CC's
# household_role check in CI is the superuser branch.
#
# /auth/setup is gated on "no superusers exist yet" and 409s otherwise.
# `compose down -v` between runs wipes the DB, so on every CI run we're
# the first user. Matches the real-prod flow (first user IS a superuser).
register_ci_user() {
  local email="$1"
  local password="$2"
  log "Registering CI user as initial superuser: $email"
  http_post "$AUTH_URL/auth/setup" \
    "" "" \
    "{\"email\":\"$email\",\"password\":\"$password\"}"
}

# Pre-flight: confirm both services are reachable. If /health fails here,
# either the port mapping is wrong or the service crashed silently after
# its healthcheck went green — both are real failure modes.
log "Probing auth + config-service /health"
curl -sf -o /dev/null "$AUTH_URL/health" || { log "FAIL: auth /health unreachable at $AUTH_URL"; exit 1; }
curl -sf -o /dev/null "$CONFIG_URL/health" || { log "FAIL: config-service /health unreachable at $CONFIG_URL"; exit 1; }
log "Both /health endpoints up"

# ---- Run ----

CC_RESPONSE=$(register_app_client "command-center" "Command Center")
log "auth response (command-center): $CC_RESPONSE"
CC_APP_KEY=$(echo "$CC_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")
log "command-center app_key captured (length=${#CC_APP_KEY})"

CFG_RESPONSE=$(register_app_client "jarvis-config-service" "Config Service")
log "auth response (config-service): $CFG_RESPONSE"
CFG_APP_KEY=$(echo "$CFG_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")
log "jarvis-config-service app_key captured (length=${#CFG_APP_KEY})"

# Belt-and-suspenders fakes registration. If config-service's POST /services
# rejects (e.g., duplicate), don't fail the whole seed — CC still has the
# legacy env-var fallback URLs in v2.3.
register_service "jarvis-llm-proxy-api" "host.docker.internal" 7705 || \
  log "WARN llm-proxy registration failed (continuing — CC falls back to env)"
register_service "jarvis-whisper-api" "host.docker.internal" 7706 || \
  log "WARN whisper registration failed (continuing — CC falls back to env)"
register_service "jarvis-tts" "host.docker.internal" 7707 || \
  log "WARN tts registration failed (continuing — CC falls back to env)"

# v2.4 — register a CI user via /auth/register. The endpoint auto-creates
# a default household and returns its ID, which is enough scaffolding for
# the subsequent node-registration step.
#
# v2.5 — node registration itself moved to a post-CC-up workflow step that
# calls CC's POST /admin/nodes. CC's endpoint also writes the local DB
# row needed by CC's verify_api_key (jarvis-command-center
# `app/deps.py:verify_api_key` lines 145-148 — when auth says valid but
# CC has no local row, verify_api_key 401s with "Node not configured
# locally"). One endpoint, both registrations.
#
# `.test` is RFC 2606 reserved and email-validator (pydantic EmailStr's
# backend) rejects it as a non-resolvable special-use TLD. `example.com`
# is the standard documentation domain and validates cleanly.
CI_USER_EMAIL="ci-node-test@example.com"
CI_USER_PASSWORD="ci-node-test-password"

USER_RESPONSE=$(register_ci_user "$CI_USER_EMAIL" "$CI_USER_PASSWORD")
log "auth response (register): $USER_RESPONSE"
CC_HOUSEHOLD_ID=$(echo "$USER_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['household_id'])")
log "household_id captured: $CC_HOUSEHOLD_ID"
# v2.13 — also capture the access_token. CASE-212 needs to call CC's
# user-JWT-gated endpoints (e.g. /nodes/{id}/settings/requests, which
# triggers an MQTT publish) and the registered user has admin/power_user
# role on their auto-created household by default.
CC_USER_JWT=$(echo "$USER_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")
log "user access_token captured (length=${#CC_USER_JWT})"

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "CC_APP_KEY=$CC_APP_KEY"
    echo "JARVIS_CC_APP_KEY=$CC_APP_KEY"
    echo "CFG_APP_KEY=$CFG_APP_KEY"
    echo "CC_HOUSEHOLD_ID=$CC_HOUSEHOLD_ID"
    echo "CC_USER_JWT=$CC_USER_JWT"
  } >> "$GITHUB_ENV"
  log "Wrote CC_APP_KEY / JARVIS_CC_APP_KEY / CFG_APP_KEY / CC_HOUSEHOLD_ID / CC_USER_JWT to GITHUB_ENV"
  log "(CC_NODE_ID + CC_NODE_KEY are set by the post-CC-up workflow step)"
else
  log "GITHUB_ENV unset — printing to stdout instead"
  echo "CC_APP_KEY=$CC_APP_KEY"
  echo "JARVIS_CC_APP_KEY=$CC_APP_KEY"
  echo "CFG_APP_KEY=$CFG_APP_KEY"
  echo "CC_HOUSEHOLD_ID=$CC_HOUSEHOLD_ID"
  echo "CC_USER_JWT=$CC_USER_JWT"
fi

log "Done"
