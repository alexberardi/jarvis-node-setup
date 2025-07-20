#!/bin/bash

set -e

echo "🔁 Refreshing Jarvis node service..."

SERVICES=("jarvis-node.service")

for service in "${SERVICES[@]}"; do
	echo "🧼 Removing old $service..."
	sudo rm -f /etc/systemd/system/$service
done

echo "📦 Re-registering services from latest script..."

# Re-create mqtt-tts.service
cat <<EOF | sudo tee /etc/systemd/system/jarvis-node.service >/dev/null
[Unit]
Description=Jarvis Node Service
After=network.target

[Service]
WorkingDirectory=/home/pi/projects/jarvis-node-setup
ExecStart=/home/pi/projects/jarvis-node-setup/venv/bin/python3 -m scripts.main
Restart=always
User=pi
Environment=PYTHONUNBUFFERED=1

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
