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

log() { echo "[seed] $*"; }

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
  status=$(curl -sS -o "$resp_file" -w "%{http_code}" -X POST "$url" \
    -H "$header_name: $header_value" \
    -H "Content-Type: application/json" \
    -d "$body")
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

if [[ -n "${GITHUB_ENV:-}" ]]; then
  {
    echo "CC_APP_KEY=$CC_APP_KEY"
    echo "JARVIS_CC_APP_KEY=$CC_APP_KEY"
    echo "CFG_APP_KEY=$CFG_APP_KEY"
  } >> "$GITHUB_ENV"
  log "Wrote CC_APP_KEY / JARVIS_CC_APP_KEY / CFG_APP_KEY to GITHUB_ENV"
else
  log "GITHUB_ENV unset — printing to stdout instead"
  echo "CC_APP_KEY=$CC_APP_KEY"
  echo "JARVIS_CC_APP_KEY=$CC_APP_KEY"
  echo "CFG_APP_KEY=$CFG_APP_KEY"
fi

log "Done"
