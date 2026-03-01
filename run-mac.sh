#!/usr/bin/env bash
# Launch jarvis-node-setup on macOS for voice demo
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Skip provisioning check (no AP mode on macOS)
export JARVIS_SKIP_PROVISIONING_CHECK=true
export CONFIG_PATH="config-mac.json"

# --- Pre-flight checks ---

# 1. portaudio (required by pyaudio)
if ! brew list portaudio &>/dev/null; then
    echo "ERROR: portaudio not installed. Run: brew install portaudio"
    exit 1
fi

# 2. pyaudio
if ! python3 -c "import pyaudio" 2>/dev/null; then
    echo "ERROR: pyaudio not installed. Run: pip install pyaudio"
    exit 1
fi

# 3. Check command-center reachability
CC_URL=$(python3 -c "import json; print(json.load(open('config-mac.json'))['jarvis_command_center_api_url'])")
if ! curl -so /dev/null --connect-timeout 2 "${CC_URL}/api/v0/health" 2>/dev/null; then
    echo "WARNING: command-center not reachable at ${CC_URL}"
    echo "  Start it with: cd ../jarvis-command-center && bash run-docker-dev.sh"
    echo "  Continuing anyway..."
fi

echo "Starting jarvis-node (macOS voice demo)..."
exec python3 scripts/main.py
