#!/bin/bash

set -e

echo "🔁 Refreshing Jarvis node service..."

SERVICES=("jarvis-node.service")

for service in "${SERVICES[@]}"; do
	echo "🧼 Removing old $service..."
	sudo rm -f /etc/systemd/system/$service
done

echo "📦 Re-registering services from latest script..."

PI_PROJECT_DIR="/home/pi/projects/jarvis-node-setup"

cat <<EOF | sudo tee /etc/systemd/system/jarvis-node.service >/dev/null
[Unit]
Description=Jarvis Node Service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=$PI_PROJECT_DIR/.venv/bin/python -m scripts.main
Restart=always
Environment=HOME=/home/pi
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=$PI_PROJECT_DIR
Environment=CONFIG_PATH=$PI_PROJECT_DIR/config.json
WorkingDirectory=$PI_PROJECT_DIR

[Install]
WantedBy=multi-user.target
EOF

echo "🔄 Reloading systemd daemon..."
sudo systemctl daemon-reexec
sudo systemctl daemon-reload

for service in "${SERVICES[@]}"; do
	sudo systemctl enable $service
	sudo systemctl restart $service
	echo "✅ $service refreshed and restarted."
done

echo "🎉 All services updated and running!"
