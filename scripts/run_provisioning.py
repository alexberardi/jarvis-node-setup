#!/usr/bin/env python3
"""
Entry point for provisioning mode.

Starts the provisioning API server for mobile app to configure the node.

Usage:
    python scripts/run_provisioning.py

Environment variables:
    JARVIS_SIMULATE_PROVISIONING: Set to "true" for simulation mode (no real WiFi)
    JARVIS_PROVISIONING_PORT: Port to run on (default: 8080)
"""

import os
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn

from provisioning.api import create_provisioning_app
from provisioning.wifi_manager import get_wifi_manager


def main() -> None:
    """Start the provisioning server."""
    # Get configuration from environment
    port = int(os.environ.get("JARVIS_PROVISIONING_PORT", "8080"))
    simulate = os.environ.get("JARVIS_SIMULATE_PROVISIONING", "false").lower()
    is_simulated = simulate in ("true", "1", "yes")

    # Get appropriate WiFi manager
    wifi_manager = get_wifi_manager()

    mode = "SIMULATION" if is_simulated else "REAL"
    print(f"[Jarvis Provisioning] Starting in {mode} mode on port {port}")

    if is_simulated:
        print("[Jarvis Provisioning] Using simulated WiFi manager")
    else:
        print("[Jarvis Provisioning] Using NetworkManager for WiFi operations")

    # On real Pi, start AP mode
    if not is_simulated:
        from provisioning.api import _get_node_id
        node_id = _get_node_id()
        ap_ssid = f"jarvis-{node_id[-8:]}"
        print(f"[Jarvis Provisioning] Starting AP mode with SSID: {ap_ssid}")
        if wifi_manager.start_ap_mode(ap_ssid):
            print(f"[Jarvis Provisioning] AP mode active. Connect to '{ap_ssid}'")
        else:
            print("[Jarvis Provisioning] Warning: Could not start AP mode")
            print("[Jarvis Provisioning] Connect to the node's IP directly")

    # Create and run the app
    app = create_provisioning_app(wifi_manager)

    print(f"[Jarvis Provisioning] API available at http://0.0.0.0:{port}/api/v1/")
    print("[Jarvis Provisioning] Endpoints:")
    print("  GET  /api/v1/info          - Node information")
    print("  GET  /api/v1/scan-networks - Available WiFi networks")
    print("  POST /api/v1/provision     - Submit WiFi credentials")
    print("  GET  /api/v1/status        - Provisioning progress")
    print("")
    print("[Jarvis Provisioning] Waiting for mobile app connection...")

    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
