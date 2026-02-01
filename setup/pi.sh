#!/bin/bash
# Raspberry Pi Zero setup script
# For production voice nodes with speaker bonnet
#
# Usage:
#   ./pi.sh              # Full setup (all dependencies)
#   ./pi.sh --provision  # Provisioning-only setup (minimal deps, faster)

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/shared.sh"

# Parse arguments
PROVISIONING_ONLY=0
for arg in "$@"; do
    case $arg in
        --provision|--provisioning|--provisioning-only)
            PROVISIONING_ONLY=1
            export JARVIS_PROVISIONING_ONLY=1
            shift
            ;;
    esac
done

if [ "$PROVISIONING_ONLY" = "1" ]; then
    echo -e "\n${BLUE}🍓 Raspberry Pi Setup (Provisioning Only)${NC}\n"
    log_info "Running minimal setup for provisioning server"
else
    echo -e "\n${BLUE}🍓 Raspberry Pi Setup${NC}\n"
fi

# Detect Pi user home directory
PI_USER="${SUDO_USER:-pi}"
PI_HOME="/home/$PI_USER"
PI_PROJECT_DIR="$PI_HOME/projects/jarvis-node-setup"

# Step 0: Configure I2S DAC (speaker bonnet) - skip for provisioning-only
if [ "$PROVISIONING_ONLY" = "1" ]; then
    log_step "Skipping I2S DAC configuration (provisioning only)"
    log_info "Audio hardware config not needed for provisioning server"
else
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
fi

# Step 1: Install system dependencies
log_step "Installing system dependencies"

log_info "Updating system..."
sudo apt-get update && sudo apt-get upgrade -y

if [ "$PROVISIONING_ONLY" = "1" ]; then
    # Minimal dependencies for provisioning server
    log_info "Installing minimal dependencies (provisioning only)..."
    sudo apt-get install -y \
        python3.11 \
        python3.11-venv \
        python3-pip \
        git \
        avahi-utils \
        hostapd \
        dnsmasq

    # Disable system services - we manage hostapd/dnsmasq ourselves
    log_info "Disabling system hostapd/dnsmasq services..."
    sudo systemctl stop hostapd dnsmasq 2>/dev/null || true
    sudo systemctl disable hostapd dnsmasq 2>/dev/null || true
    sudo systemctl mask hostapd dnsmasq 2>/dev/null || true

    log_success "Minimal system dependencies installed"
else
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
fi
log_info "Python 3.11 version: $(python3.11 --version)"

# Step 2: Python venv (use Python 3.11 on Pi)
setup_python_venv "$PI_PROJECT_DIR/.venv" "python3.11"

# Step 3: Config files
setup_config
setup_env

# Step 4: Database (skip for provisioning-only)
if [ "$PROVISIONING_ONLY" = "1" ]; then
    log_step "Skipping database setup (provisioning only)"
    log_info "Database migrations not needed for provisioning server"
else
    setup_database "$PI_PROJECT_DIR/.venv"
fi

# Step 5: Configure ALSA audio (skip for provisioning-only)
if [ "$PROVISIONING_ONLY" = "1" ]; then
    log_step "Skipping audio configuration (provisioning only)"
    log_info "Audio setup not needed for provisioning server"
else
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
fi

# Step 6: Create systemd services
log_step "Creating systemd services"

if [ "$PROVISIONING_ONLY" = "1" ]; then
    # Only create provisioning service for provisioning-only setup
    cat <<EOF | sudo tee /etc/systemd/system/jarvis-provisioning.service
[Unit]
Description=Jarvis Node Provisioning Service
After=network.target

[Service]
ExecStart=$PI_PROJECT_DIR/.venv/bin/python $PI_PROJECT_DIR/scripts/run_provisioning.py
Restart=on-failure
User=$PI_USER
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$PI_PROJECT_DIR

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable jarvis-provisioning.service
    log_success "Provisioning service created and enabled"
else
    # Main jarvis-node service (runs after provisioning)
    cat <<EOF | sudo tee /etc/systemd/system/jarvis-node.service
[Unit]
Description=Jarvis Node Service
After=network.target

[Service]
ExecStart=$PI_PROJECT_DIR/.venv/bin/python $PI_PROJECT_DIR/scripts/main.py
Restart=always
User=$PI_USER
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$PI_PROJECT_DIR

[Install]
WantedBy=multi-user.target
EOF

    # Provisioning service (runs on first boot / when not provisioned)
    cat <<EOF | sudo tee /etc/systemd/system/jarvis-provisioning.service
[Unit]
Description=Jarvis Node Provisioning Service
After=network.target

[Service]
ExecStart=$PI_PROJECT_DIR/.venv/bin/python $PI_PROJECT_DIR/scripts/run_provisioning.py
Restart=on-failure
User=$PI_USER
Environment=PYTHONUNBUFFERED=1
WorkingDirectory=$PI_PROJECT_DIR

[Install]
WantedBy=multi-user.target
EOF

    sudo systemctl daemon-reload
    sudo systemctl enable jarvis-node.service
    # Note: jarvis-provisioning.service is NOT enabled by default
    # Enable it manually for first-time setup: sudo systemctl enable --now jarvis-provisioning.service

    log_success "Systemd services created (jarvis-node enabled, jarvis-provisioning available)"
fi

# Step 7: Verify
verify_installation "$PI_PROJECT_DIR/.venv"

# Done
if [ "$PROVISIONING_ONLY" = "1" ]; then
    print_completion "Raspberry Pi (Provisioning Only)"

    echo "📡 Local IP address: $(hostname -I | cut -d' ' -f1)"
    echo ""
    echo "The provisioning service is enabled and will start on boot."
    echo ""
    echo "To start now:"
    echo "  sudo systemctl start jarvis-provisioning.service"
    echo ""
    echo "To view logs:"
    echo "  sudo journalctl -u jarvis-provisioning.service -f"
    echo ""
    echo "After provisioning completes, run full setup:"
    echo "  ./setup/pi.sh"
    echo ""
else
    print_completion "Raspberry Pi"

    echo "📡 Local IP address: $(hostname -I | cut -d' ' -f1)"
    echo ""
    echo "For first-time setup (provisioning via mobile app):"
    echo "  sudo systemctl start jarvis-provisioning.service"
    echo "  # Connect to jarvis-XXXX AP from mobile app"
    echo ""
    echo "After provisioning, start the main service:"
    echo "  sudo systemctl stop jarvis-provisioning.service"
    echo "  sudo systemctl start jarvis-node.service"
    echo ""
    echo "To view logs:"
    echo "  sudo journalctl -u jarvis-node.service -f"
    echo "  sudo journalctl -u jarvis-provisioning.service -f"
    echo ""
    log_warn "Please reboot to activate the I2S DAC: sudo reboot"
fi
