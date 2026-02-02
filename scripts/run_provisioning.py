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
import signal
import sys
import threading

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

load_dotenv()

import uvicorn
from jarvis_log_client import init as init_logging, JarvisLogger

from provisioning.api import create_provisioning_app
from provisioning.wifi_manager import get_wifi_manager
from utils.encryption_utils import initialize_encryption_key, get_secret_dir

# Global flag for shutdown
_shutdown_event = threading.Event()

# Initialize logging
init_logging(
    app_id=os.getenv("JARVIS_APP_ID", "jarvis-provisioning"),
    app_key=os.getenv("JARVIS_APP_KEY", ""),
)
logger = JarvisLogger(service="jarvis-provisioning")


def _is_raspberry_pi() -> bool:
    """Check if running on a Raspberry Pi."""
    try:
        with open("/sys/firmware/devicetree/base/model", "r") as f:
            return "raspberry pi" in f.read().lower()
    except (FileNotFoundError, PermissionError):
        return False


def _trigger_shutdown() -> None:
    """Trigger graceful shutdown of the provisioning server."""
    logger.info("Provisioning complete, triggering server shutdown...")
    _shutdown_event.set()
    # Send SIGTERM to self to stop uvicorn gracefully
    os.kill(os.getpid(), signal.SIGTERM)


def run_provisioning_server(auto_shutdown: bool = False) -> bool:
    """
    Start the provisioning server.

    Args:
        auto_shutdown: If True, server will automatically shutdown after
                      successful provisioning. Used when called from main.py.

    Returns:
        True if provisioning completed successfully, False otherwise.
    """
    # Initialize K1 encryption key if not present (required for K2 storage)
    secret_dir = get_secret_dir()
    print(f"[provisioning] Secret directory: {secret_dir}")
    logger.info("Initializing encryption key", secret_dir=str(secret_dir))
    initialize_encryption_key()
    print("[provisioning] ✅ Encryption key (K1) ready")

    # Get configuration from environment
    port = int(os.environ.get("JARVIS_PROVISIONING_PORT", "8080"))
    simulate = os.environ.get("JARVIS_SIMULATE_PROVISIONING", "false").lower()
    is_simulated = simulate in ("true", "1", "yes")

    # Auto-detect hostapd backend on Pi if not explicitly set
    backend = os.environ.get("JARVIS_WIFI_BACKEND", "").lower()
    if not backend and _is_raspberry_pi() and not is_simulated:
        os.environ["JARVIS_WIFI_BACKEND"] = "hostapd"
        print("[provisioning] Auto-detected Raspberry Pi, using hostapd backend")
        logger.info("Auto-detected Raspberry Pi, using hostapd backend")

    # Get appropriate WiFi manager
    wifi_manager = get_wifi_manager()

    mode = "SIMULATION" if is_simulated else "REAL"
    backend_name = os.environ.get("JARVIS_WIFI_BACKEND", "networkmanager")
    print(f"[provisioning] Starting server mode={mode} port={port} backend={backend_name}")
    logger.info("Starting provisioning server", mode=mode, port=port, backend=backend_name)

    if is_simulated:
        print("[provisioning] Using simulated WiFi manager")
        logger.info("Using simulated WiFi manager")
    else:
        print(f"[provisioning] Using {backend_name} for WiFi operations")
        logger.info(f"Using {backend_name} for WiFi operations")

    # On real Pi, scan networks BEFORE entering AP mode, then start AP
    if not is_simulated:
        from provisioning.api import _get_node_id
        node_id = _get_node_id()
        ap_ssid = f"jarvis-{node_id[-8:]}"

        # Scan and cache networks BEFORE entering AP mode
        # (WiFi adapter can't scan while acting as an AP)
        print("[provisioning] Scanning for available networks...")
        logger.info("Scanning networks before AP mode")
        networks = wifi_manager.scan_and_cache()
        print(f"[provisioning] Found {len(networks)} networks")
        logger.info("Network scan complete", count=len(networks))
        for net in networks[:5]:  # Log first 5
            print(f"  - {net.ssid} ({net.signal_strength} dBm)")

        # Now start AP mode
        print(f"[provisioning] Starting AP mode with SSID: {ap_ssid}")
        logger.info("Starting AP mode", ssid=ap_ssid)
        if wifi_manager.start_ap_mode(ap_ssid):
            print(f"[provisioning] ✅ AP mode active: {ap_ssid}")
            logger.info("AP mode active", ssid=ap_ssid)
        else:
            print(f"[provisioning] ⚠️ Could not start AP mode")
            logger.warning("Could not start AP mode, connect to node IP directly")

    # Set up shutdown callback if auto_shutdown is enabled
    on_provisioned = _trigger_shutdown if auto_shutdown else None

    # Create and run the app
    app = create_provisioning_app(wifi_manager, on_provisioned=on_provisioned)

    logger.info("API available", url=f"http://0.0.0.0:{port}/api/v1/")
    logger.info("Waiting for mobile app connection...")

    if auto_shutdown:
        print("[provisioning] Auto-shutdown enabled: server will stop after provisioning")
        logger.info("Auto-shutdown enabled")

    uvicorn.run(app, host="0.0.0.0", port=port)

    # Return True if shutdown was triggered by successful provisioning
    return _shutdown_event.is_set()


def main() -> None:
    """Start the provisioning server (standalone mode)."""
    run_provisioning_server(auto_shutdown=False)


if __name__ == "__main__":
    main()
