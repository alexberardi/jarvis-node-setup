#!/bin/bash
# Install jarvis-provisioning systemd service
# Run with: sudo bash setup/install-provisioning-service.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_FILE="$SCRIPT_DIR/jarvis-provisioning.service"

echo "Installing jarvis-provisioning service..."

# Copy service file
sudo cp "$SERVICE_FILE" /etc/systemd/system/

# Reload systemd
sudo systemctl daemon-reload

# Enable service to start on boot
sudo systemctl enable jarvis-provisioning

# Start the service now
sudo systemctl start jarvis-provisioning

echo "Done! Service status:"
sudo systemctl status jarvis-provisioning --no-pager

echo ""
echo "Useful commands:"
echo "  sudo systemctl status jarvis-provisioning  # Check status"
echo "  sudo systemctl restart jarvis-provisioning # Restart"
echo "  sudo journalctl -u jarvis-provisioning -f  # View logs"
echo "  sudo systemctl disable jarvis-provisioning # Disable auto-start"
