#!/bin/bash
# macOS setup script
# For development machines

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/shared.sh"

echo -e "\n${BLUE}🍎 macOS Setup${NC}\n"

# Step 1: Check for Homebrew
log_step "Checking prerequisites"

if ! command -v brew &> /dev/null; then
    log_error "Homebrew not found. Please install it first:"
    echo '  /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    exit 1
fi
log_success "Homebrew found"

# Check for Python
if ! command -v python3 &> /dev/null; then
    log_info "Installing Python via Homebrew..."
    brew install python@3.11
fi
log_success "Python found: $(python3 --version)"

# Step 2: Install system dependencies
log_step "Installing system dependencies"

log_info "Installing dependencies via Homebrew..."
brew install \
    portaudio \
    sox \
    ffmpeg \
    sqlcipher \
    mosquitto \
    espeak \
    || true  # Continue even if some are already installed

log_success "System dependencies installed"

# Step 3: Python venv
setup_python_venv "$PROJECT_ROOT/venv" "python3"

# Step 4: Config files
setup_config
setup_env

# Step 5: Database
setup_database "$PROJECT_ROOT/venv"

# Step 6: Verify
verify_installation "$PROJECT_ROOT/venv"

# macOS-specific: Audio permissions notice
log_step "macOS Audio Permissions"
log_warn "macOS requires microphone permissions for voice capture."
log_info "When you run the app, grant microphone access if prompted."
log_info "You can also enable it in: System Settings → Privacy & Security → Microphone"

# Done
print_completion "macOS"

echo "For development, you can run:"
echo "  source venv/bin/activate"
echo "  python scripts/main.py"
echo ""
