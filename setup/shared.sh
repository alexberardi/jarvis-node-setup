#!/bin/bash
# Shared setup functions for all platforms

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Get the project root directory (parent of setup/)
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

log_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

log_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

log_warn() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

log_error() {
    echo -e "${RED}❌ $1${NC}"
}

log_step() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}📦 $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"
}

# Create Python virtual environment
setup_python_venv() {
    local venv_path="${1:-$PROJECT_ROOT/venv}"
    local python_cmd="${2:-python3}"

    log_step "Setting up Python virtual environment"

    if [ -d "$venv_path" ]; then
        log_info "Virtual environment already exists at $venv_path"
    else
        log_info "Creating virtual environment..."
        $python_cmd -m venv "$venv_path"
        log_success "Virtual environment created"
    fi

    # Activate and upgrade pip
    source "$venv_path/bin/activate"
    log_info "Upgrading pip..."
    pip install --upgrade pip --quiet

    # Install requirements (prefer platform-specific file)
    # Priority: provisioning-only > Pi-specific > generic
    local req_file=""
    if [ "${JARVIS_PROVISIONING_ONLY:-0}" = "1" ] && [ -f "$PROJECT_ROOT/requirements-provisioning.txt" ]; then
        req_file="$PROJECT_ROOT/requirements-provisioning.txt"
        log_info "Using minimal provisioning requirements (JARVIS_PROVISIONING_ONLY=1)"
    elif [ -f "$PROJECT_ROOT/requirements-pi.txt" ] && grep -q "Raspberry Pi" /sys/firmware/devicetree/base/model 2>/dev/null; then
        req_file="$PROJECT_ROOT/requirements-pi.txt"
    elif [ -f "$PROJECT_ROOT/requirements.txt" ]; then
        req_file="$PROJECT_ROOT/requirements.txt"
    fi

    if [ -n "$req_file" ]; then
        log_info "Installing requirements from $(basename $req_file)..."
        pip install -r "$req_file" --quiet
        log_success "Requirements installed"
    else
        log_warn "No requirements file found"
    fi
}

# Setup config.json from example
setup_config() {
    log_step "Setting up configuration"

    local config_file="$PROJECT_ROOT/config.json"
    local example_file="$PROJECT_ROOT/config.example.json"

    if [ -f "$config_file" ]; then
        log_success "config.json already exists"
    elif [ -f "$example_file" ]; then
        cp "$example_file" "$config_file"
        log_success "Created config.json from example"
        log_warn "Please update config.json with your settings"
    else
        log_warn "No config.example.json found, skipping config setup"
    fi
}

# Setup .env from example
setup_env() {
    log_step "Setting up environment variables"

    local env_file="$PROJECT_ROOT/.env"
    local example_file="$PROJECT_ROOT/.env.example"

    if [ -f "$env_file" ]; then
        log_success ".env already exists"
    elif [ -f "$example_file" ]; then
        cp "$example_file" "$env_file"
        log_success "Created .env from example"
        log_warn "Please update .env with your settings"
    else
        log_warn "No .env.example found, skipping .env setup"
    fi
}

# Run database migrations
setup_database() {
    log_step "Setting up database"

    local venv_path="${1:-$PROJECT_ROOT/venv}"

    # Ensure venv is activated
    source "$venv_path/bin/activate"

    if [ -f "$PROJECT_ROOT/alembic.ini" ]; then
        log_info "Running database migrations..."
        cd "$PROJECT_ROOT"
        alembic upgrade head
        log_success "Database migrations complete"
    else
        log_warn "No alembic.ini found, skipping migrations"
    fi
}

# Verify installation
verify_installation() {
    log_step "Verifying installation"

    local venv_path="${1:-$PROJECT_ROOT/venv}"
    source "$venv_path/bin/activate"

    # Check Python
    log_info "Python version: $(python --version)"

    # Check key packages
    if python -c "import sqlalchemy" 2>/dev/null; then
        log_success "SQLAlchemy installed"
    else
        log_error "SQLAlchemy not found"
    fi

    if python -c "import paho.mqtt.client" 2>/dev/null; then
        log_success "paho-mqtt installed"
    else
        log_warn "paho-mqtt not found (optional for MQTT features)"
    fi
}

# Print completion message
print_completion() {
    local os_name="$1"

    echo -e "\n${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}🎉 Setup complete for $os_name!${NC}"
    echo -e "${GREEN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}\n"

    echo "Next steps:"
    echo "  1. Update config.json with your settings"
    echo "  2. Update .env with your secrets"
    echo "  3. Activate the venv: source .venv/bin/activate"
    echo "  4. Run: python scripts/main.py"
    echo ""
}
