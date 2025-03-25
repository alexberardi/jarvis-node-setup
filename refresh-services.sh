#!/bin/bash

set -e

echo "🔁 Refreshing Jarvis node services..."

SERVICES=("voice-listener.service" "mqtt-tts.service")

for service in "${SERVICES[@]}"; do
  echo "🧼 Removing old $service..."
  sudo rm -f /etc/systemd/system/$service
done

echo "📦 Re-registering services from latest script..."

# Re-create mqtt-tts.service
cat <<EOF | sudo tee /etc/systemd/system/mqtt-tts.service > /dev/null
[Unit]
Description=MQTT Listener for TTS Confirmation
After=network.target

[Service]
ExecStart=/home/pi/projects/jarvis-node-setup/venv/bin/python /home/pi/projects/jarvis-node-setup/scripts/mqtt_tts_listener.py
Restart=always
User=pi
WorkingDirectory=/home/pi/projects/jarvis-node-setup
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Re-create voice-listener.service
cat <<EOF | sudo tee /etc/systemd/system/voice-listener.service > /dev/null
[Unit]
Description=Jarvis Voice Wake Word Listener
After=network.target sound.target

[Service]
ExecStart=/home/pi/projects/jarvis-node-setup/venv/bin/python /home/pi/projects/jarvis-node-setup/scripts/voice_listener.py
Restart=always
User=pi
WorkingDirectory=/home/pi/projects/jarvis-node-setup
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

echo "🔄 Reloading systemd daemon..."
sudo systemctl daemon-reexec

for service in "${SERVICES[@]}"; do
  sudo systemctl enable $service
  sudo systemctl restart $service
  echo "✅ $service refreshed and restarted."
done

echo "🎉 All services updated and running!"

