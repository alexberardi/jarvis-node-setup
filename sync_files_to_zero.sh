#!/bin/bash

# Fast file sync to Pi Zero - just copy files, no installation
# Usage: ./sync_files_to_zero.sh [--files-only]

set -e

FILES_ONLY=false
if [[ "$1" == "--files-only" || "$1" == "-f" ]]; then
    FILES_ONLY=true
fi

# Configuration
REMOTE_HOST="zero-office.local"
REMOTE_USER="pi"
REMOTE_DIR="/home/pi/projects/jarvis-node-setup"
LOCAL_DIR="."

echo "🚀 Fast sync to zero-office.local (files only)..."

# Create remote directory if it doesn't exist
ssh ${REMOTE_USER}@${REMOTE_HOST} "mkdir -p ${REMOTE_DIR}"

# Sync files using rsync (exclude common files that don't need syncing)
rsync -avz --delete \
    --exclude='venv/' \
    --exclude='.venv/' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='.git/' \
    --exclude='.gitignore' \
    --exclude='*.log' \
    --exclude='out.wav' \
    --exclude='network_discovery_results.json' \
    --exclude='runtime_fingerprint_index.json' \
    --exclude='ha_fingerprints.json' \
    --exclude='.pytest_cache/' \
    --exclude='htmlcov/' \
    --exclude='.coverage' \
    --exclude='.DS_Store' \
    --exclude='*.tmp' \
    --exclude='*.swp' \
    --exclude='*.swo' \
    --exclude='*.db' \
    --exclude='.env' \
    --exclude='config.json' \
    --exclude='config-mac.json' \
    ${LOCAL_DIR}/ ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/

echo "✅ Fast sync complete!"
echo "📁 Files copied to: ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"

if [[ "$FILES_ONLY" == false ]]; then
    # One-time cleanup: remove stale root state and corrupt DB from HOME mismatch
    echo "🧹 Cleaning up stale root state (if any)..."
    ssh ${REMOTE_USER}@${REMOTE_HOST} "sudo rm -rf /root/.jarvis/ && rm -f ${REMOTE_DIR}/jarvis_node.db" 2>/dev/null || true

    echo "🔊 Applying ALSA config..."
    ssh ${REMOTE_USER}@${REMOTE_HOST} "sudo bash ${REMOTE_DIR}/setup/fix-alsa.sh"

    echo "🔄 Refreshing systemd service..."
    ssh ${REMOTE_USER}@${REMOTE_HOST} "cd ${REMOTE_DIR} && bash refresh-services.sh"
fi
