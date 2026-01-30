#!/bin/bash
# Ubuntu Desktop setup script
# For development machines, not Pi nodes

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/shared.sh"

echo -e "\n${BLUE}🐧 Ubuntu Desktop Setup${NC}\n"

# Step 1: Install system dependencies
log_step "Installing system dependencies"

log_info "Updating package list..."
sudo apt-get update

log_info "Installing dependencies..."
sudo apt-get install -y \
    python3 \
    python3-venv \
    python3-pip \
    git \
    alsa-utils \
    espeak \
    mosquitto-clients \
    portaudio19-dev \
    sox \
    ffmpeg \
    sqlcipher \
    libsqlcipher-dev \
    avahi-utils

log_success "System dependencies installed"

# Step 2: Python venv
setup_python_venv "$PROJECT_ROOT/venv" "python3"

# Step 3: Config files
setup_config
setup_env

# Step 4: Database
setup_database "$PROJECT_ROOT/venv"

# Step 5: Verify
verify_installation "$PROJECT_ROOT/venv"

# Done
print_completion "Ubuntu Desktop"

echo "For development, you can run:"
echo "  source venv/bin/activate"
echo "  python scripts/main.py"
echo ""
echo "Note: This is a development setup. For production Pi nodes,"
echo "      use setup.sh and select 'Raspberry Pi'."
