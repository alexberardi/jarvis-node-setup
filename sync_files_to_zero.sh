#!/bin/bash

# Fast file sync to Pi Zero - just copy files, no installation
# Usage: ./sync_files_to_zero.sh

set -e

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
    ${LOCAL_DIR}/ ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/

echo "✅ Fast sync complete!"
echo "📁 Files copied to: ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/" 