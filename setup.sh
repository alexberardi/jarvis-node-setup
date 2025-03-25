#!/bin/bash

set -e

echo "🔊 [0/6] Configuring I2S DAC (speaker bonnet)..."

CONFIG_FILE="/boot/firmware/config.txt"
if ! grep -q "dtoverlay=hifiberry-dac" "$CONFIG_FILE"; then
  sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG_FILE"
  echo "dtoverlay=hifiberry-dac" | sudo tee -a "$CONFIG_FILE"
  echo "✅ I2S DAC overlay added to config.txt"
else
  echo "✅ I2S DAC already configured"
fi


echo "🔧 [1/6] Updating system..."
sudo apt update && sudo apt upgrade -y

echo "📦 [2/6] Installing dependencies..."
sudo apt install -y python3 python3-pip python3-venv git alsa-utils espeak mosquitto-clients neovim python3-pyaudio portaudio19-dev

echo "🐍 [3/6] Creating Python venv and installing requirements..."
if [ ! -d ~/projects/jarvis-node-setup/venv ]; then
  python3 -m venv ~/projects/jarvis-node-setup/venv
fi

source ~/projects/jarvis-node-setup/venv/bin/activate
pip install --upgrade pip
pip install paho-mqtt httpx pvporcupine pyaudio

echo "📝 [4/6] Preparing config..."
if [ ! -f ~/projects/jarvis-node-setup/config.json ]; then
  cp ~/projects/jarvis-node-setup/config.example.json ~/projects/jarvis-node-setup/config.json
  echo "📁 config.json created from example — be sure to update it."
else
  echo "✅ config.json already exists, skipping."
fi


echo "🎧 [5/6] Setting default audio output..."

cat <<EOF > /home/pi/.asoundrc
defaults.pcm.card 0
defaults.pcm.device 0
defaults.ctl.card 0
EOF

chown pi:pi /home/pi/.asoundrc


echo "🔁 [6/6] Creating systemd service..."

cat <<EOF | sudo tee /etc/systemd/system/mqtt-tts.service
[Unit]
Description=Jarvis MQTT TTS Listener
After=network.target

[Service]
ExecStart=/home/pi/projects/jarvis-node-setup/venv/bin/python /home/pi/projects/jarvis-node-setup/scripts/mqtt_tts_listener.py
Restart=always
User=pi
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=/home/pi/projects/jarvis-node-setup

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reexec
sudo systemctl enable mqtt-tts.service
sudo systemctl restart mqtt-tts.service


echo "🔁 Creating voice listener systemd service..."

cat <<EOF | sudo tee /etc/systemd/system/voice-listener.service
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

sudo systemctl daemon-reexec
sudo systemctl enable voice-listener.service
sudo systemctl restart voice-listener.service



echo "📡 Local IP address: $(hostname -I | cut -d' ' -f1)"
echo "✅ Setup complete. Jarvis node is now running and listening."
echo "⚠️ Please reboot to activate the I2S DAC: sudo reboot"




