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
from jarvis_log_client import init as init_logging, JarvisLogger

from provisioning.api import create_provisioning_app
from provisioning.wifi_manager import get_wifi_manager

# Initialize logging
init_logging(
    app_id=os.getenv("JARVIS_APP_ID", "jarvis-provisioning"),
    app_key=os.getenv("JARVIS_APP_KEY", ""),
)
logger = JarvisLogger(service="jarvis-provisioning")


def main() -> None:
    """Start the provisioning server."""
    # Get configuration from environment
    port = int(os.environ.get("JARVIS_PROVISIONING_PORT", "8080"))
    simulate = os.environ.get("JARVIS_SIMULATE_PROVISIONING", "false").lower()
    is_simulated = simulate in ("true", "1", "yes")

    # Get appropriate WiFi manager
    wifi_manager = get_wifi_manager()

    mode = "SIMULATION" if is_simulated else "REAL"
    logger.info("Starting provisioning server", mode=mode, port=port)

    if is_simulated:
        logger.info("Using simulated WiFi manager")
    else:
        logger.info("Using NetworkManager for WiFi operations")

    # On real Pi, start AP mode
    if not is_simulated:
        from provisioning.api import _get_node_id
        node_id = _get_node_id()
        ap_ssid = f"jarvis-{node_id[-8:]}"
        logger.info("Starting AP mode", ssid=ap_ssid)
        if wifi_manager.start_ap_mode(ap_ssid):
            logger.info("AP mode active", ssid=ap_ssid)
        else:
            logger.warning("Could not start AP mode, connect to node IP directly")

    # Create and run the app
    app = create_provisioning_app(wifi_manager)

    logger.info("API available", url=f"http://0.0.0.0:{port}/api/v1/")
    logger.info("Waiting for mobile app connection...")

    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
