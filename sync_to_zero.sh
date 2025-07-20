#!/bin/bash

PI_HOST="zero-office.local"
PI_USER="pi"
REMOTE_PATH="/home/pi/projects/jarvis-node-setup"
LOCAL_PATH="$(cd "$(dirname "$0")"; pwd)"

echo "🔁 Syncing local code to $PI_HOST (without deleting remote files)..."

rsync -az \
  --exclude ".git" \
  --exclude "__pycache__" \
  --exclude ".DS_Store" \
  --exclude "venv" \
  "$LOCAL_PATH/" "${PI_USER}@${PI_HOST}:${REMOTE_PATH}/"

echo "🔧 Running setup.sh on $PI_HOST..."

ssh "${PI_USER}@${PI_HOST}" "cd ${REMOTE_PATH} && chmod +x ./setup.sh && ./setup.sh"

echo "🚀 Running refresh-services.sh on $PI_HOST..."

ssh "${PI_USER}@${PI_HOST}" "cd ${REMOTE_PATH} && chmod +x ./refresh-services.sh && ./refresh-services.sh"

echo "✅ Sync + setup + deploy complete!"