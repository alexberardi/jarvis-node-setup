#!/bin/bash

set -e

echo "🔁 Refreshing Jarvis node service..."

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICES=("jarvis-node.service")

for service in "${SERVICES[@]}"; do
	echo "🧼 Removing old $service..."
	sudo rm -f /etc/systemd/system/$service
done

echo "📦 Re-registering services from latest template..."

PI_USER="${SUDO_USER:-pi}"
PI_HOME="/home/$PI_USER"
PI_PROJECT_DIR="$PI_HOME/projects/jarvis-node-setup"

sed -e "s|__VENV__|$PI_PROJECT_DIR/.venv|g" \
    -e "s|__PROJECT_DIR__|$PI_PROJECT_DIR|g" \
    -e "s|__HOME__|$PI_HOME|g" \
    "$SCRIPT_DIR/setup/jarvis-node.service" | sudo tee /etc/systemd/system/jarvis-node.service > /dev/null

echo "🔄 Reloading systemd daemon..."
sudo systemctl daemon-reexec
sudo systemctl daemon-reload

for service in "${SERVICES[@]}"; do
	sudo systemctl enable $service
	sudo systemctl restart $service
	echo "✅ $service refreshed and restarted."
done

echo "🎉 All services updated and running!"
