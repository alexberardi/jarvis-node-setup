#!/bin/bash

PI_HOST="zero-office.local"
PI_USER="pi"
REMOTE_PATH="/home/pi/projects/jarvis-node-setup"
LOCAL_PATH="$(cd "$(dirname "$0")"; pwd)"

echo "🔁 Syncing local code to $PI_HOST (files only)..."
./sync_files_to_zero.sh

# Check if setup is needed (only run if setup.sh was modified or first time)
echo "🔍 Checking if setup is needed..."
if ssh "${PI_USER}@${PI_HOST}" "test ! -f ${REMOTE_PATH}/.setup_complete" || [ "./setup.sh" -nt "./.setup_timestamp" ]; then
    echo "🔧 Running setup.sh on $PI_HOST..."
    ssh "${PI_USER}@${PI_HOST}" "cd ${REMOTE_PATH} && chmod +x ./setup.sh && ./setup.sh"
    # Mark setup as complete
    ssh "${PI_USER}@${PI_HOST}" "touch ${REMOTE_PATH}/.setup_complete"
    touch ./.setup_timestamp
else
    echo "✅ Setup already complete, skipping..."
fi

echo "🚀 Running refresh-services.sh on $PI_HOST..."

ssh "${PI_USER}@${PI_HOST}" "cd ${REMOTE_PATH} && chmod +x ./refresh-services.sh && ./refresh-services.sh"

echo "✅ Sync + setup + deploy complete!"