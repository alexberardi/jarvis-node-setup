#!/bin/bash
# Install jarvis-provisioning systemd service
# Run with: bash setup/install-provisioning-service.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
SERVICE_TEMPLATE="$SCRIPT_DIR/jarvis-provisioning.service"

# Detect current user (don't use root even if run with sudo)
CURRENT_USER="${SUDO_USER:-$USER}"
SECRET_DIR="/home/$CURRENT_USER/.jarvis"

echo "Installing jarvis-provisioning service..."
echo "  Project directory: $PROJECT_DIR"
echo "  User: $CURRENT_USER"
echo "  Secret directory: $SECRET_DIR"

# Generate service file with correct paths
sed -e "s|__WORKING_DIR__|$PROJECT_DIR|g" \
    -e "s|__USER__|$CURRENT_USER|g" \
    -e "s|__SECRET_DIR__|$SECRET_DIR|g" \
    "$SERVICE_TEMPLATE" | sudo tee /etc/systemd/system/jarvis-provisioning.service > /dev/null

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
