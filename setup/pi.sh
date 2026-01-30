#!/bin/bash
# Raspberry Pi Zero setup script
# For production voice nodes with speaker bonnet

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/shared.sh"

echo -e "\n${BLUE}🍓 Raspberry Pi Setup${NC}\n"

# Detect Pi user home directory
PI_USER="${SUDO_USER:-pi}"
PI_HOME="/home/$PI_USER"
PI_PROJECT_DIR="$PI_HOME/projects/jarvis-node-setup"

# Step 0: Configure I2S DAC (speaker bonnet)
log_step "Configuring I2S DAC (speaker bonnet)"

CONFIG_FILE="/boot/firmware/config.txt"

if [ ! -f "$CONFIG_FILE" ]; then
    # Try legacy location
    CONFIG_FILE="/boot/config.txt"
fi

if [ -f "$CONFIG_FILE" ]; then
    if ! grep -q "dtoverlay=hifiberry-dac" "$CONFIG_FILE"; then
        sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG_FILE"
        echo "dtoverlay=hifiberry-dac" | sudo tee -a "$CONFIG_FILE"
        log_success "I2S DAC overlay added to config.txt"
        log_warn "Reboot required to activate DAC"
    else
        log_success "I2S DAC already configured"
    fi
else
    log_error "Could not find config.txt - is this a Raspberry Pi?"
    exit 1
fi

# Step 1: Install system dependencies
log_step "Installing system dependencies"

log_info "Updating system..."
sudo apt-get update && sudo apt-get upgrade -y

log_info "Installing dependencies..."
sudo apt-get install -y \
    python3.11 \
    python3.11-venv \
    python3-pip \
    python3-venv \
    git \
    alsa-utils \
    espeak \
    mosquitto-clients \
    python3-pyaudio \
    portaudio19-dev \
    sox \
    ffmpeg \
    sqlcipher \
    libsqlcipher-dev \
    avahi-utils \
    libopenblas-dev

log_success "System dependencies installed"
log_info "Python 3.11 version: $(python3.11 --version)"

# Step 2: Python venv (use Python 3.11 on Pi)
setup_python_venv "$PI_PROJECT_DIR/venv" "python3.11"

# Step 3: Config files
setup_config
setup_env

# Step 4: Database
setup_database "$PI_PROJECT_DIR/venv"

# Step 5: Configure ALSA audio
log_step "Configuring audio system"

# Lock HifiBerry DAC as card 0, USB mic as card 1
sudo tee /etc/modprobe.d/alsa-base.conf > /dev/null <<EOF
options snd_soc_hifiberry_dac index=0
options snd_usb_audio index=1
EOF

# Set /etc/asound.conf with correct playback and capture config
sudo tee /etc/asound.conf > /dev/null <<EOF
# Output (speaker)
defaults.pcm.card 0
defaults.pcm.device 0
defaults.ctl.card 0

# Input (microphone) via dsnoop
pcm.dsnoopmic {
  type dsnoop
  ipc_key 87654321
  slave {
    pcm "hw:1,0"
    channels 1
  }
}

pcm.!default {
  type asym
  playback.pcm "hw:0,0"
  capture.pcm "dsnoopmic"
}
EOF

log_success "ALSA system config created"

# Detect USB microphone
log_info "Detecting USB microphone..."
USB_MIC_CARD=$(arecord -l 2>/dev/null | grep -i "usb" | sed -n 's/.*card \([0-9]*\):.*/\1/p' | head -n 1)

if [[ -n "$USB_MIC_CARD" ]]; then
    log_success "USB mic detected as card $USB_MIC_CARD"

    cat >> "$PI_HOME/.asoundrc" <<EOF

# Input (mic)
defaults.capture.card $USB_MIC_CARD
defaults.capture.device 0
EOF

    chown "$PI_USER:$PI_USER" "$PI_HOME/.asoundrc"
else
    log_warn "No USB mic found — skipping capture default setup"
fi

# Step 6: Create systemd service
log_step "Creating systemd service"

cat <<EOF | sudo tee /etc/systemd/system/jarvis-node.service
[Unit]
Description=Jarvis Node Service
After=network.target

[Service]
ExecStart=$PI_PROJECT_DIR/venv/bin/python $PI_PROJECT_DIR/scripts/main.py
Restart=always
User=$PI_USER
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$PI_PROJECT_DIR

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable jarvis-node.service

log_success "Systemd service created and enabled"

# Step 7: Verify
verify_installation "$PI_PROJECT_DIR/venv"

# Done
print_completion "Raspberry Pi"

echo "📡 Local IP address: $(hostname -I | cut -d' ' -f1)"
echo ""
echo "To start the service now:"
echo "  sudo systemctl start jarvis-node.service"
echo ""
echo "To view logs:"
echo "  sudo journalctl -u jarvis-node.service -f"
echo ""
log_warn "Please reboot to activate the I2S DAC: sudo reboot"
