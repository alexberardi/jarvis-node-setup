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
    log_step "Configuring audio for ReSpeaker 2-mics Pi HAT v2.0 (TLV320AIC3104)"

    CONFIG_FILE="/boot/firmware/config.txt"
    if [ ! -f "$CONFIG_FILE" ]; then
        CONFIG_FILE="/boot/config.txt"
    fi

    OVERLAY_SRC="$SCRIPT_DIR/respeaker-2mic-v2_0-overlay.dts"
    OVERLAY_DEST_DIR="/boot/firmware/overlays"
    if [ ! -d "$OVERLAY_DEST_DIR" ] && [ -d "/boot/overlays" ]; then
        OVERLAY_DEST_DIR="/boot/overlays"
    fi

    if [ -f "$OVERLAY_SRC" ]; then
        log_info "Compiling respeaker-2mic-v2_0 overlay"
        TMP_DTBO="$(mktemp --suffix=.dtbo)"
        if ! command -v dtc >/dev/null 2>&1; then
            sudo apt-get install -y device-tree-compiler >/dev/null
        fi
        if dtc -W no-unit_address_vs_reg -I dts "$OVERLAY_SRC" -o "$TMP_DTBO" >/dev/null 2>&1; then
            sudo cp "$TMP_DTBO" "$OVERLAY_DEST_DIR/respeaker-2mic-v2_0-overlay.dtbo"
            log_success "Overlay installed to $OVERLAY_DEST_DIR"
        else
            log_error "Failed to compile $OVERLAY_SRC"
        fi
        rm -f "$TMP_DTBO"
    else
        log_warn "Overlay source missing at $OVERLAY_SRC"
    fi

    if [ -f "$CONFIG_FILE" ]; then
        sudo sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG_FILE"
        for old in hifiberry-dac wm8960-soundcard; do
            if grep -q "^dtoverlay=${old}\$" "$CONFIG_FILE"; then
                sudo sed -i "s|^dtoverlay=${old}\$|#dtoverlay=${old} # replaced for ReSpeaker v2.0|" "$CONFIG_FILE"
                log_info "Disabled previous overlay: ${old}"
            fi
        done
        grep -q "^dtoverlay=respeaker-2mic-v2_0-overlay" "$CONFIG_FILE" || \
            echo "dtoverlay=respeaker-2mic-v2_0-overlay" | sudo tee -a "$CONFIG_FILE" > /dev/null
        grep -q "^dtparam=spi=on" "$CONFIG_FILE" || \
            echo "dtparam=spi=on" | sudo tee -a "$CONFIG_FILE" > /dev/null
        grep -q "^dtparam=i2c_arm=on" "$CONFIG_FILE" || \
            echo "dtparam=i2c_arm=on" | sudo tee -a "$CONFIG_FILE" > /dev/null
        log_success "Boot config updated"
        log_warn "Reboot required for overlay to load"
    else
        log_error "Could not find config.txt - is this a Raspberry Pi?"
        exit 1
    fi
fi

# Step 0.5: Disable desktop GUI (headless voice node doesn't need it)
CURRENT_TARGET=$(systemctl get-default 2>/dev/null || echo "unknown")
if [ "$CURRENT_TARGET" = "graphical.target" ]; then
    log_step "Disabling desktop GUI (saves ~20-30 MB RAM)"
    sudo systemctl set-default multi-user.target
    log_success "Boot target set to multi-user.target (headless)"
    log_info "GUI will stop on next reboot (or run: sudo systemctl isolate multi-user.target)"
else
    log_step "Desktop GUI already disabled ($CURRENT_TARGET)"
fi

# Step 1: Install system dependencies
log_step "Installing system dependencies"

log_info "Updating system..."
sudo apt-get update && sudo apt-get upgrade -y

# Detect Python package names — Bookworm ships 3.11 as "python3", not "python3.11"
if apt-cache show python3.11 >/dev/null 2>&1; then
    PY_PKG="python3.11"
    PY_VENV_PKG="python3.11-venv"
    PY_BIN="python3.11"
else
    PY_PKG="python3"
    PY_VENV_PKG="python3-venv"
    PY_BIN="python3"
fi
log_info "Using Python package: ${PY_PKG}"

if [ "$PROVISIONING_ONLY" = "1" ]; then
    # Minimal dependencies for provisioning server
    log_info "Installing minimal dependencies (provisioning only)..."
    sudo apt-get install -y \
        "${PY_PKG}" \
        "${PY_VENV_PKG}" \
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
        "${PY_PKG}" \
        "${PY_VENV_PKG}" \
        python3-pip \
        git \
        alsa-utils \
        espeak \
        mosquitto-clients \
        python3-pyaudio \
        portaudio19-dev \
        sox \
        ffmpeg \
        mpv \
        sqlcipher \
        libsqlcipher-dev \
        avahi-utils \
        libopenblas-dev \
        hostapd \
        dnsmasq \
        pulseaudio \
        pulseaudio-module-bluetooth \
        libspeexdsp1 \
        libwebrtc-audio-processing-1-3

    # Disable system services - we manage hostapd/dnsmasq ourselves
    log_info "Disabling system hostapd/dnsmasq services..."
    sudo systemctl stop hostapd dnsmasq 2>/dev/null || true
    sudo systemctl disable hostapd dnsmasq 2>/dev/null || true
    sudo systemctl mask hostapd dnsmasq 2>/dev/null || true

    log_success "System dependencies installed"
fi

# BlueZ Class of Device — make iOS list jarvis-dev as an audio device
# in the BT picker. Without this it classifies us as "Computer" and
# either hides us from the audio output list or won't route A2DP.
if grep -q "^#Class = 0x000100" /etc/bluetooth/main.conf 2>/dev/null; then
    log_info "Setting BlueZ Class of Device to audio (0x240404)..."
    sudo sed -i 's/^#Class = 0x000100/Class = 0x240404/' /etc/bluetooth/main.conf
    sudo systemctl restart bluetooth 2>/dev/null || true
    log_success "BlueZ Class set"
fi

log_info "Python version: $(${PY_BIN} --version)"

# Step 2: Python venv
setup_python_venv "$PI_PROJECT_DIR/.venv" "${PY_BIN}"

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

    # TLV320AIC3104 (ReSpeaker v2.0) is the only audio card we need; pin via name.
    sudo tee /etc/modprobe.d/alsa-base.conf > /dev/null <<EOF
options snd-soc-tlv320aic3x index=0
EOF

    # Aliases `output` and `dsnoopmic` are referenced from the app code
    # (config.example.json `mic_device_name="dsnoopmic"`, platform_abstraction
    # uses `aplay -D output`). Keep these stable across hardware changes.
    sudo tee /etc/asound.conf > /dev/null <<EOF
pcm.softvol {
  type softvol
  slave.pcm "plughw:CARD=seeed2micvoicec,DEV=0"
  control {
    name "SoftMaster"
    card seeed2micvoicec
  }
}

pcm.output {
  type plug
  slave.pcm "softvol"
}

pcm.dsnoopmic_hw {
  type dsnoop
  ipc_key 87654321
  slave {
    pcm "hw:CARD=seeed2micvoicec,DEV=0"
    channels 2
    rate 48000
    format S16_LE
  }
}

pcm.dsnoopmic {
  type plug
  slave.pcm "dsnoopmic_hw"
}

pcm.!default {
  type asym
  playback.pcm "softvol"
  capture.pcm "dsnoopmic"
}
EOF

    log_success "ALSA system config created"

    # Mixer baseline — JST speaker is wired to HP path; defaults leave it muted.
    if command -v amixer >/dev/null 2>&1 && aplay -l 2>/dev/null | grep -qi seeed2micvoicec; then
        log_info "Applying TLV320AIC3104 mixer baseline..."
        # JST speaker on v2.0 is wired to LLOUT/RLOUT, not HP. Use Line
        # path at conservative levels; HP paths kept muted. See
        # install.sh's configure_audio() for the calibration notes.
        amixer -c seeed2micvoicec sset 'PCM' '100%' unmute               2>/dev/null || true
        amixer -c seeed2micvoicec sset 'Line' '0' unmute                 2>/dev/null || true
        amixer -c seeed2micvoicec sset 'Line DAC' '78' unmute            2>/dev/null || true
        amixer -c seeed2micvoicec sset 'Left Line Mixer DACL1' on        2>/dev/null || true
        amixer -c seeed2micvoicec sset 'Right Line Mixer DACR1' on       2>/dev/null || true
        amixer -c seeed2micvoicec sset 'HP' '0' mute                     2>/dev/null || true
        amixer -c seeed2micvoicec sset 'HP DAC' '0' mute                 2>/dev/null || true
        amixer -c seeed2micvoicec sset 'HPCOM' '0' mute                  2>/dev/null || true
        amixer -c seeed2micvoicec sset 'HPCOM DAC' '0' mute              2>/dev/null || true
        amixer -c seeed2micvoicec sset 'PGA' '60%'                       2>/dev/null || true
        amixer -c seeed2micvoicec sset 'Left PGA Mixer Line1L' on        2>/dev/null || true
        amixer -c seeed2micvoicec sset 'Right PGA Mixer Line1R' on       2>/dev/null || true
        sudo alsactl store 2>/dev/null || true
        log_success "TLV320AIC3104 mixer baseline applied"
    else
        log_info "Card not present yet (overlay loads on reboot) — re-run setup after reboot to apply mixer baseline"
    fi

    # Group memberships for SPI (APA102) / GPIO (button) / I2C (Grove)
    for grp in spi gpio i2c; do
        if getent group "$grp" >/dev/null 2>&1; then
            sudo usermod -aG "$grp" "$PI_USER" 2>/dev/null || true
        fi
    done
fi

# Step 6: Create systemd service
log_step "Creating systemd service"

# Clean up old provisioning service if it exists (from previous setup versions)
if [ -f /etc/systemd/system/jarvis-provisioning.service ]; then
    log_info "Removing old jarvis-provisioning.service (no longer needed)"
    sudo systemctl stop jarvis-provisioning.service 2>/dev/null || true
    sudo systemctl disable jarvis-provisioning.service 2>/dev/null || true
    sudo rm -f /etc/systemd/system/jarvis-provisioning.service
fi

# Single jarvis-node service that handles both provisioning and normal
# operation. Runs as $PI_USER (not root) so per-user PulseAudio works
# for Bluetooth audio routing — see docs/migration-to-pi-service.md.
# AP-mode provisioning (hostapd/dnsmasq) escalates via sudoers.d.
PI_UID="$(id -u "$PI_USER")"
sed -e "s|__VENV__|$PI_PROJECT_DIR/.venv|g" \
    -e "s|__PROJECT_DIR__|$PI_PROJECT_DIR|g" \
    -e "s|__SERVICE_USER__|$PI_USER|g" \
    -e "s|__SERVICE_HOME__|$PI_HOME|g" \
    -e "s|__SERVICE_UID__|$PI_UID|g" \
    "$SCRIPT_DIR/jarvis-node.service" | sudo tee /etc/systemd/system/jarvis-node.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable jarvis-node.service

log_success "Systemd service created and enabled (jarvis-node.service)"

# Step 7: Verify
verify_installation "$PI_PROJECT_DIR/.venv"

# Done
if [ "$PROVISIONING_ONLY" = "1" ]; then
    print_completion "Raspberry Pi (Provisioning Only)"

    echo "📡 Local IP address: $(hostname -I | cut -d' ' -f1)"
    echo ""
    echo "The jarvis-node service is enabled and will start on boot."
    echo "On first boot, it will automatically enter provisioning mode."
    echo ""
    echo "To start now:"
    echo "  sudo systemctl start jarvis-node.service"
    echo ""
    echo "To view logs:"
    echo "  sudo journalctl -u jarvis-node.service -f"
    echo ""
    echo "After provisioning completes via mobile app, run full setup:"
    echo "  ./setup/pi.sh"
    echo ""
else
    print_completion "Raspberry Pi"

    echo "📡 Local IP address: $(hostname -I | cut -d' ' -f1)"
    echo ""
    echo "The jarvis-node service is enabled and will start on boot."
    echo ""
    echo "On first boot (not provisioned):"
    echo "  - Node automatically enters AP mode (jarvis-XXXX)"
    echo "  - Connect with mobile app to provision"
    echo "  - After provisioning, service auto-restarts in normal mode"
    echo ""
    echo "To start now:"
    echo "  sudo systemctl start jarvis-node.service"
    echo ""
    echo "To view logs:"
    echo "  sudo journalctl -u jarvis-node.service -f"
    echo ""
    log_warn "Please reboot to activate the I2S DAC: sudo reboot"
fi
