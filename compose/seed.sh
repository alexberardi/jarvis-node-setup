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
#
# The seed is idempotent against re-runs in the same CI job (the workflow
# does `compose down -v` between runs); duplicate POSTs to the admin APIs
# would 409 on a second invocation, which is fine — we ignore conflicts
# explicitly only for fakes registration where re-running locally is more
# likely.

set -euo pipefail

AUTH_URL="${AUTH_URL:-http://localhost:7701}"
CONFIG_URL="${CONFIG_URL:-http://localhost:7700}"
AUTH_ADMIN_TOKEN="${AUTH_ADMIN_TOKEN:-ci-auth-admin-token}"
# CONFIG_ADMIN_TOKEN is only used by config-service's POST /services; the
# bulk /v1/services/register endpoint accepts the auth admin token.
CONFIG_ADMIN_TOKEN="${CONFIG_ADMIN_TOKEN:-ci-auth-admin-token}"

log() { echo "[seed] $*"; }

# Register an app-client in jarvis-auth and return the generated key.
# Auth generates the key; we capture it.
register_app_client() {
  local app_id="$1"
  local name="$2"
  log "Registering app-client: $app_id"
  curl -sf -X POST "$AUTH_URL/admin/app-clients" \
    -H "X-Jarvis-Admin-Token: $AUTH_ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"app_id\":\"$app_id\",\"name\":\"$name\"}"
}

# Register a service URL in jarvis-config-service. host.docker.internal
# from inside containers reaches the runner host (where the fake LLM and
# fake Whisper processes listen on 7705 / 7706).
register_service() {
  local name="$1"
  local host="$2"
  local port="$3"
  log "Registering service: $name → $host:$port"
  curl -sf -X POST "$CONFIG_URL/services" \
    -H "X-Admin-Token: $CONFIG_ADMIN_TOKEN" \
    -H "Content-Type: application/json" \
    -d "{\"name\":\"$name\",\"host\":\"$host\",\"port\":$port,\"scheme\":\"http\",\"health_path\":\"/health\"}"
}

# ---- Run ----

CC_RESPONSE=$(register_app_client "command-center" "Command Center")
CC_APP_KEY=$(echo "$CC_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")
log "command-center app_key captured (length=${#CC_APP_KEY})"

CFG_RESPONSE=$(register_app_client "jarvis-config-service" "Config Service")
CFG_APP_KEY=$(echo "$CFG_RESPONSE" | python3 -c "import json,sys; print(json.load(sys.stdin)['key'])")
log "jarvis-config-service app_key captured (length=${#CFG_APP_KEY})"

# Fakes — registering them in config-service so CC's service discovery
# returns the right URLs at runtime. The compose stack already wires CC
# to them via legacy env vars too, so this is belt-and-suspenders for now.
register_service "jarvis-llm-proxy-api" "host.docker.internal" 7705 || \
  log "WARN llm-proxy registration failed (may already exist)"
register_service "jarvis-whisper-api" "host.docker.internal" 7706 || \
  log "WARN whisper registration failed (may already exist)"

# Emit to GITHUB_ENV so subsequent steps see them. The compose file reads
# JARVIS_CC_APP_KEY when bringing up CC; subsequent test steps use
# CC_APP_KEY directly to call auth /internal/validate-app.
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
