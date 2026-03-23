#!/bin/bash
# Jarvis Node Installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/alexberardi/jarvis-node-setup/main/install.sh | sudo bash
#
# Flags:
#   --no-audio     Skip ALSA / I2S DAC configuration
#   --force        Reinstall even if already at latest version
#   --version TAG  Install a specific version (e.g. v0.1.0)
#   --local        Skip download (tarball already extracted to /opt/jarvis-node)

set -euo pipefail

REPO="alexberardi/jarvis-node-setup"
INSTALL_DIR="/opt/jarvis-node"
SERVICE_NAME="jarvis-node"

# --- Defaults ---
SKIP_AUDIO=0
FORCE=0
TARGET_VERSION=""
LOCAL_MODE=0

# --- Colors ---
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { printf "${BLUE}>${NC} %s\n" "$1"; }
success() { printf "${GREEN}>${NC} %s\n" "$1"; }
error()   { printf "${RED}>${NC} %s\n" "$1" >&2; exit 1; }
warn()    { printf "${RED}>${NC} %s\n" "$1" >&2; }

# --- Parse arguments ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-audio)    SKIP_AUDIO=1; shift ;;
    --force)       FORCE=1; shift ;;
    --version)     TARGET_VERSION="$2"; shift 2 ;;
    --local)       LOCAL_MODE=1; shift ;;
    *)             warn "Unknown flag: $1"; shift ;;
  esac
done

# --- Preflight checks ---
preflight() {
  printf "\n${BOLD}Jarvis Node Installer${NC}\n\n"

  # Must be root
  if [ "$(id -u)" -ne 0 ]; then
    error "This installer must be run as root (use sudo)"
  fi

  # Must be Linux
  if [ "$(uname -s)" != "Linux" ]; then
    error "This installer is for Linux (Raspberry Pi). Got: $(uname -s)"
  fi

  # Detect architecture
  ARCH="$(uname -m)"
  case "$ARCH" in
    armv7l|armv6l) ARCH="armv7l" ;;
    aarch64)       ARCH="arm64" ;;
    *)             error "Unsupported architecture: $ARCH (expected armv7l or arm64)" ;;
  esac
  info "Architecture: ${ARCH}"

  # Detect Pi model (informational)
  if [ -f /sys/firmware/devicetree/base/model ]; then
    PI_MODEL="$(tr -d '\0' < /sys/firmware/devicetree/base/model)"
    info "Hardware: ${PI_MODEL}"
  fi

  # Check for curl
  if ! command -v curl >/dev/null 2>&1; then
    error "curl is required but not installed. Run: apt-get install curl"
  fi

  # Ensure swap is available — Pi Zero 2W has only 512MB RAM which is
  # not enough for extracting large tarballs or pip-compiling packages.
  if [ "$(swapon --show --noheadings | wc -l)" -eq 0 ]; then
    info "No swap detected — creating 1GB swapfile..."
    fallocate -l 1G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=1024 status=none
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    # Persist across reboots
    if ! grep -q '/swapfile' /etc/fstab; then
      echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    success "Swap enabled (1GB, persistent)"
  fi
}

# --- Version check ---
get_version() {
  if [ -n "$TARGET_VERSION" ]; then
    # Strip leading v if present
    VERSION="${TARGET_VERSION#v}"
    TAG="v${VERSION}"
    info "Target version: ${TAG}"
    return
  fi

  info "Checking latest version..."
  TAG="$(curl -fsSL "https://api.github.com/repos/${REPO}/releases/latest" \
    | grep '"tag_name"' | head -1 | sed 's/.*"tag_name": *"\([^"]*\)".*/\1/')"

  if [ -z "$TAG" ]; then
    error "Could not determine latest version. Check https://github.com/${REPO}/releases"
  fi

  VERSION="${TAG#v}"
  info "Latest version: ${TAG}"

  # Check if already installed at this version
  if [ "$FORCE" -eq 0 ] && [ -f "${INSTALL_DIR}/VERSION" ]; then
    INSTALLED="$(cat "${INSTALL_DIR}/VERSION")"
    if [ "$INSTALLED" = "$VERSION" ]; then
      success "Already at version ${VERSION}. Use --force to reinstall."
      exit 0
    fi
    info "Upgrading from ${INSTALLED} to ${VERSION}"
  fi
}

# --- Detect Python ---
detect_python() {
  # Raspberry Pi OS Bookworm ships Python 3.11 as "python3" without a
  # versioned "python3.11" package.  Detect which form is available so
  # apt-get doesn't fail on a fresh install.
  if apt-cache show python3.11 >/dev/null 2>&1; then
    PY_PKG="python3.11"
    PY_VENV_PKG="python3.11-venv"
    PY_BIN="python3.11"
  else
    PY_PKG="python3"
    PY_VENV_PKG="python3-venv"
    PY_BIN="python3"
  fi
  info "Python package: ${PY_PKG} ($(${PY_BIN} --version 2>&1))"
}

# --- Install system packages ---
install_apt_deps() {
  info "Installing system dependencies..."

  # Only update if cache is stale (> 1 hour)
  local apt_cache="/var/cache/apt/pkgcache.bin"
  if [ ! -f "$apt_cache" ] || [ "$(( $(date +%s) - $(stat -c %Y "$apt_cache" 2>/dev/null || echo 0) ))" -gt 3600 ]; then
    apt-get update -qq
  fi

  detect_python

  # Core packages (always needed)
  apt-get install -y --no-install-recommends -qq \
    "${PY_PKG}" \
    "${PY_VENV_PKG}" \
    git \
    avahi-utils \
    hostapd \
    dnsmasq \
    sqlcipher \
    libsqlcipher-dev \
    libopenblas-dev \
    > /dev/null

  # Audio packages (unless --no-audio)
  if [ "$SKIP_AUDIO" -eq 0 ]; then
    apt-get install -y --no-install-recommends -qq \
      alsa-utils \
      portaudio19-dev \
      sox \
      ffmpeg \
      espeak \
      > /dev/null
  fi

  # MQTT client for TTS
  apt-get install -y --no-install-recommends -qq \
    mosquitto-clients \
    > /dev/null

  # Disable system hostapd/dnsmasq (node manages them directly)
  systemctl stop hostapd dnsmasq 2>/dev/null || true
  systemctl disable hostapd dnsmasq 2>/dev/null || true
  systemctl mask hostapd dnsmasq 2>/dev/null || true

  success "System dependencies installed"
}

# --- Download and extract tarball ---
download_and_extract() {
  if [ "$LOCAL_MODE" -eq 1 ]; then
    info "Local mode: skipping download (using existing ${INSTALL_DIR})"
    return
  fi

  local tarball="jarvis-node-${VERSION}-${ARCH}.tar.gz"
  local url="https://github.com/${REPO}/releases/download/${TAG}/${tarball}"

  # Back up existing install
  if [ -d "$INSTALL_DIR" ]; then
    local backup="${INSTALL_DIR}.bak"
    info "Backing up existing install to ${backup}"
    rm -rf "$backup"
    mv "$INSTALL_DIR" "$backup"
  fi

  # Stream directly to tar — avoids buffering the full tarball in /tmp
  # (which is tmpfs / RAM-backed) and prevents OOM on 512MB boards.
  info "Downloading and extracting ${tarball}..."
  if ! curl -fSL "$url" | tar xzf - -C /; then
    error "Download/extract failed. Check: https://github.com/${REPO}/releases/tag/${TAG}"
  fi

  success "Extracted to ${INSTALL_DIR}"
}

# --- Configure audio ---
configure_audio() {
  if [ "$SKIP_AUDIO" -eq 1 ]; then
    info "Skipping audio configuration (--no-audio)"
    return
  fi

  info "Configuring audio..."

  # --- I2S DAC overlay (HifiBerry speaker bonnet) ---
  local config_file="/boot/firmware/config.txt"
  if [ ! -f "$config_file" ]; then
    config_file="/boot/config.txt"
  fi

  if [ -f "$config_file" ]; then
    if ! grep -q "dtoverlay=hifiberry-dac" "$config_file"; then
      sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$config_file"
      echo "dtoverlay=hifiberry-dac" >> "$config_file"
      success "I2S DAC overlay added (reboot required to activate)"
      NEEDS_REBOOT=1
    else
      info "I2S DAC already configured"
    fi
  else
    warn "Could not find config.txt — skipping I2S DAC setup"
  fi

  # --- Lock card order: HifiBerry=0, USB mic=1 ---
  cat > /etc/modprobe.d/alsa-base.conf <<'ALSA_MOD'
options snd_soc_hifiberry_dac index=0
options snd_usb_audio index=1
ALSA_MOD

  # --- ALSA system config ---
  cat > /etc/asound.conf <<'ASOUND'
# Output (speaker) with software volume control
defaults.pcm.card 0
defaults.pcm.device 0
defaults.ctl.card 0

pcm.softvol {
  type softvol
  slave.pcm "plughw:0,0"
  control {
    name "SoftMaster"
    card 0
  }
}

pcm.output {
  type plug
  slave.pcm "softvol"
}

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
  playback.pcm "softvol"
  capture.pcm "dsnoopmic"
}
ASOUND

  success "ALSA configuration written"

  # --- Detect USB microphone ---
  local usb_mic_card
  usb_mic_card="$(arecord -l 2>/dev/null | grep -i "usb" | sed -n 's/.*card \([0-9]*\):.*/\1/p' | head -n 1 || true)"

  if [ -n "$usb_mic_card" ]; then
    success "USB microphone detected as card ${usb_mic_card}"
  else
    warn "No USB microphone detected — plug one in and reboot"
  fi
}

# --- Set up config and env files ---
setup_config() {
  # config.json
  if [ ! -f "${INSTALL_DIR}/config.json" ]; then
    cp "${INSTALL_DIR}/config.example.json" "${INSTALL_DIR}/config.json"
    info "Created config.json (will be filled during provisioning)"
  else
    info "config.json already exists, preserving"
  fi

  # .env
  if [ ! -f "${INSTALL_DIR}/.env" ]; then
    if [ -f "${INSTALL_DIR}/.env.example" ]; then
      cp "${INSTALL_DIR}/.env.example" "${INSTALL_DIR}/.env"
      info "Created .env"
    else
      touch "${INSTALL_DIR}/.env"
      info "Created empty .env (no .env.example in release)"
    fi
  else
    info ".env already exists, preserving"
  fi
}

# --- Fix or rebuild venv if bundled Python doesn't match system ---
rebuild_venv() {
  local venv_python="${INSTALL_DIR}/.venv/bin/python"
  local pyvenv_cfg="${INSTALL_DIR}/.venv/pyvenv.cfg"

  # If the bundled venv works, nothing to do
  if [ -x "$venv_python" ] && "$venv_python" --version >/dev/null 2>&1; then
    info "Bundled venv OK ($(${venv_python} --version 2>&1))"
    return
  fi

  # Fast path: if the Python major.minor matches, just fix the symlinks.
  # The CI builds the venv with /usr/local/bin/python3 (Docker) but the
  # Pi has /usr/bin/python3 — the compiled packages are still compatible.
  if [ -f "$pyvenv_cfg" ]; then
    local bundled_ver
    bundled_ver="$(grep '^version' "$pyvenv_cfg" | sed 's/.*= *//' | cut -d. -f1-2)"
    local system_ver
    system_ver="$("${PY_BIN}" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")"

    if [ "$bundled_ver" = "$system_ver" ]; then
      info "Python ${system_ver} matches — fixing venv symlinks..."
      local system_python_home
      system_python_home="$(dirname "$(readlink -f "$(which "${PY_BIN}")")")"

      # Update pyvenv.cfg home path
      sed -i "s|^home = .*|home = ${system_python_home}|" "$pyvenv_cfg"

      # Repoint the python symlinks
      rm -f "${INSTALL_DIR}/.venv/bin/python" "${INSTALL_DIR}/.venv/bin/python3"
      ln -s "${system_python_home}/python3" "${INSTALL_DIR}/.venv/bin/python"
      ln -s "${system_python_home}/python3" "${INSTALL_DIR}/.venv/bin/python3"

      if "${INSTALL_DIR}/.venv/bin/python" --version >/dev/null 2>&1; then
        success "Venv fixed ($(${INSTALL_DIR}/.venv/bin/python --version 2>&1))"
        return
      fi
      warn "Symlink fix failed — falling back to full rebuild"
    fi
  fi

  # Slow path: full rebuild from requirements
  info "Bundled venv incompatible with system Python — rebuilding..."

  # Use disk-backed tmpdir so pip doesn't fill the tmpfs
  export TMPDIR="${INSTALL_DIR}/tmp"
  mkdir -p "$TMPDIR"

  rm -rf "${INSTALL_DIR}/.venv"
  "${PY_BIN}" -m venv "${INSTALL_DIR}/.venv"
  "${INSTALL_DIR}/.venv/bin/python" -m pip install --upgrade pip --quiet

  # Pick the right requirements file
  local req_file="${INSTALL_DIR}/requirements-pi.txt"
  if [ ! -f "$req_file" ]; then
    req_file="${INSTALL_DIR}/requirements-base.txt"
  fi
  if [ ! -f "$req_file" ]; then
    warn "No requirements file found — venv will have no packages"
    return
  fi

  # Strip onnxruntime from requirements — on armv7l there is no cp313 wheel
  # so it must be attempted separately to avoid blocking the whole install.
  local tmp_req="${TMPDIR}/jarvis-requirements.txt"
  grep -v "^onnxruntime" "$req_file" > "$tmp_req" || true

  local pip_args=""
  if [ "$ARCH" = "armv7l" ]; then
    pip_args="--extra-index-url https://www.piwheels.org/simple"
  fi

  info "Installing Python packages (this may take a few minutes on Pi Zero)..."
  if ! "${INSTALL_DIR}/.venv/bin/python" -m pip install \
    -r "$tmp_req" \
    $pip_args \
    --quiet 2>&1; then
    warn "Some packages failed to install — node may have reduced functionality"
  fi

  # onnxruntime: arm64 has PyPI wheels, armv7l may not
  info "Installing onnxruntime (best-effort)..."
  "${INSTALL_DIR}/.venv/bin/python" -m pip install \
    "onnxruntime>=1.16.0" \
    $pip_args \
    --quiet 2>/dev/null \
    || warn "onnxruntime unavailable — wake word detection will be disabled"

  # openwakeword (installed without deps to avoid pulling ai-edge-litert)
  "${INSTALL_DIR}/.venv/bin/python" -m pip install \
    openwakeword --no-deps \
    $pip_args \
    --quiet 2>/dev/null || warn "openwakeword install failed (non-fatal)"

  # jarvis-command-sdk (bundled in tarball)
  if [ -d "${INSTALL_DIR}/jarvis-command-sdk" ]; then
    "${INSTALL_DIR}/.venv/bin/python" -m pip install \
      "${INSTALL_DIR}/jarvis-command-sdk" \
      --quiet 2>/dev/null || warn "jarvis-command-sdk install failed (non-fatal)"
  fi

  rm -rf "$TMPDIR"
  unset TMPDIR
  success "Venv rebuilt with system Python ($(${INSTALL_DIR}/.venv/bin/python --version 2>&1))"
}

# --- Run database migrations ---
setup_database() {
  info "Running database migrations..."

  cd "$INSTALL_DIR"
  if ! "${INSTALL_DIR}/.venv/bin/python" -m alembic upgrade head 2>/dev/null; then
    # If migration fails (encrypted DB with lost key), back up and retry
    local db_file="${INSTALL_DIR}/jarvis_node.db"
    if [ -f "$db_file" ]; then
      local backup="${db_file}.bak.$(date +%Y%m%d%H%M%S)"
      warn "Migration failed — backing up database to $(basename "$backup")"
      cp "$db_file" "$backup"
      rm -f "$db_file"
      "${INSTALL_DIR}/.venv/bin/python" -m alembic upgrade head
    fi
  fi

  success "Database ready"
}

# --- Register built-in commands ---
register_commands() {
  info "Registering built-in commands..."
  cd "$INSTALL_DIR"
  if "${INSTALL_DIR}/.venv/bin/python" -m scripts.install_command --all --skip-deps 2>/dev/null; then
    success "Commands registered"
  else
    warn "Command registration failed (non-fatal, will retry on first boot)"
  fi
}

# --- Create systemd service ---
create_service() {
  info "Creating systemd service..."

  # Clean up old provisioning service from previous setup versions
  if [ -f /etc/systemd/system/jarvis-provisioning.service ]; then
    systemctl stop jarvis-provisioning.service 2>/dev/null || true
    systemctl disable jarvis-provisioning.service 2>/dev/null || true
    rm -f /etc/systemd/system/jarvis-provisioning.service
  fi

  cat > "/etc/systemd/system/${SERVICE_NAME}.service" <<EOF
[Unit]
Description=Jarvis Node Service
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=${INSTALL_DIR}/.venv/bin/python -m scripts.main
Restart=always
Environment=HOME=/root
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONPATH=${INSTALL_DIR}
Environment=CONFIG_PATH=${INSTALL_DIR}/config.json
WorkingDirectory=${INSTALL_DIR}

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable "${SERVICE_NAME}.service"

  success "Systemd service created and enabled"
}

# --- Start the service ---
start_service() {
  info "Starting ${SERVICE_NAME}..."
  systemctl restart "${SERVICE_NAME}.service"
  success "Service started"
}

# --- Verify ---
verify() {
  info "Verifying installation..."

  local python="${INSTALL_DIR}/.venv/bin/python"
  info "Python: $($python --version)"
  info "Version: $(cat "${INSTALL_DIR}/VERSION")"

  # Quick smoke test
  if $python -c "import sqlalchemy; import paho.mqtt.client; import fastapi" 2>/dev/null; then
    success "Core packages verified"
  else
    warn "Some packages may be missing — check logs"
  fi
}

# --- Print success ---
print_success() {
  printf "\n"
  printf "${GREEN}${BOLD}Jarvis Node installed successfully!${NC}\n"
  printf "\n"
  printf "  Version:  %s\n" "$VERSION"
  printf "  Location: %s\n" "$INSTALL_DIR"
  printf "\n"

  if [ "${NEEDS_REBOOT:-0}" -eq 1 ]; then
    printf "  ${BOLD}Reboot required${NC} to activate the I2S DAC:\n"
    printf "    sudo reboot\n"
    printf "\n"
    printf "  After reboot, the node will start automatically in provisioning\n"
    printf "  mode. Connect with the Jarvis mobile app to complete setup.\n"
  else
    printf "  The node is running in provisioning mode.\n"
    printf "  Connect with the Jarvis mobile app to complete setup.\n"
  fi

  printf "\n"
  printf "  View logs:\n"
  printf "    sudo journalctl -u ${SERVICE_NAME} -f\n"
  printf "\n"
}

# --- Main ---
main() {
  NEEDS_REBOOT=0

  preflight
  get_version
  install_apt_deps
  download_and_extract
  configure_audio
  setup_config
  rebuild_venv
  setup_database
  register_commands
  create_service
  start_service
  verify
  print_success
}

main
