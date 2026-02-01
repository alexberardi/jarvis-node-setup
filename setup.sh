#!/bin/bash
# Jarvis Node Setup - Main Entry Point
# Detects or asks for OS and runs the appropriate setup script

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SETUP_DIR="$SCRIPT_DIR/setup"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}"
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║                                                           ║"
echo "║     🤖 Jarvis Node Setup                                  ║"
echo "║                                                           ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo -e "${NC}"

# Try to auto-detect the OS
detect_os() {
    if [[ "$OSTYPE" == "darwin"* ]]; then
        echo "macos"
    elif [[ -f /etc/os-release ]]; then
        source /etc/os-release
        if [[ "$ID" == "raspbian" ]] || [[ "$ID" == "debian" && -f /sys/firmware/devicetree/base/model ]]; then
            # Check if it's actually a Pi
            if grep -q "Raspberry Pi" /sys/firmware/devicetree/base/model 2>/dev/null; then
                echo "pi"
            else
                echo "ubuntu"
            fi
        elif [[ "$ID" == "ubuntu" ]]; then
            echo "ubuntu"
        else
            echo "unknown"
        fi
    else
        echo "unknown"
    fi
}

# Show menu and get selection
show_menu() {
    local detected="$1"
    local recommended=""

    case "$detected" in
        macos)   recommended=" (detected)" ;;
        ubuntu)  recommended=" (detected)" ;;
        pi)      recommended=" (detected)" ;;
    esac

    echo -e "Select your platform:\n"
    echo -e "  ${CYAN}1)${NC} 🍓 Raspberry Pi     - Production voice node with speaker bonnet"
    [[ "$detected" == "pi" ]] && echo -e "     ${GREEN}$recommended${NC}"

    echo -e "  ${CYAN}2)${NC} 🐧 Ubuntu Desktop   - Development machine"
    [[ "$detected" == "ubuntu" ]] && echo -e "     ${GREEN}$recommended${NC}"

    echo -e "  ${CYAN}3)${NC} 🍎 macOS            - Development machine"
    [[ "$detected" == "macos" ]] && echo -e "     ${GREEN}$recommended${NC}"

    echo -e "  ${CYAN}q)${NC} ❌ Quit\n"
}

# Main
detected_os=$(detect_os)

# Collect extra args to pass through (e.g., --provision)
extra_args=()

if [[ -n "$1" ]]; then
    # OS passed as argument
    choice="$1"
    shift
    extra_args=("$@")
else
    # Interactive mode
    show_menu "$detected_os"

    # Set default based on detection
    case "$detected_os" in
        pi)     default="1" ;;
        ubuntu) default="2" ;;
        macos)  default="3" ;;
        *)      default="" ;;
    esac

    if [[ -n "$default" ]]; then
        read -p "Enter choice [$default]: " choice
        choice="${choice:-$default}"
    else
        read -p "Enter choice: " choice
    fi
fi

# Run the appropriate setup script
case "$choice" in
    1|pi|raspberry)
        echo -e "\n${GREEN}Running Raspberry Pi setup...${NC}\n"
        bash "$SETUP_DIR/pi.sh" "${extra_args[@]}"
        ;;
    2|ubuntu|linux)
        echo -e "\n${GREEN}Running Ubuntu setup...${NC}\n"
        bash "$SETUP_DIR/ubuntu.sh" "${extra_args[@]}"
        ;;
    3|macos|mac|darwin)
        echo -e "\n${GREEN}Running macOS setup...${NC}\n"
        bash "$SETUP_DIR/macos.sh" "${extra_args[@]}"
        ;;
    q|quit|exit)
        echo "Setup cancelled."
        exit 0
        ;;
    *)
        echo -e "${RED}Invalid choice: $choice${NC}"
        echo "Usage: $0 [pi|ubuntu|macos] [--provision]"
        exit 1
        ;;
esac
