"""
FastAPI application for provisioning API.

Provides endpoints for the mobile app to provision headless Pi Zero nodes.
"""

import json
import os
import platform
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, BackgroundTasks

from provisioning.models import (
    NetworkInfo,
    NodeInfo,
    ProvisioningState,
    ProvisionRequest,
    ProvisionResponse,
    ProvisionStatus,
    ScanNetworksResponse,
)
from provisioning.registration import register_with_command_center
from provisioning.startup import mark_provisioned
from provisioning.state_machine import ProvisioningStateMachine
from provisioning.wifi_credentials import save_wifi_credentials
from provisioning.wifi_manager import WiFiManager


# Version of the provisioning firmware
FIRMWARE_VERSION = "1.0.0"


def _get_mac_address() -> str:
    """Get the MAC address of the primary network interface."""
    try:
        # Try to get MAC from /sys/class/net on Linux
        for iface in ["wlan0", "eth0", "en0"]:
            path = Path(f"/sys/class/net/{iface}/address")
            if path.exists():
                return path.read_text().strip()
    except (OSError, IOError):
        pass

    # Fallback: generate a consistent ID from UUID
    return f"00:00:{uuid.getnode():012x}"[-17:].replace(":", ":")


def _get_hardware_type() -> str:
    """Determine the hardware type."""
    machine = platform.machine().lower()
    system = platform.system().lower()

    if "arm" in machine and system == "linux":
        # Check for Pi Zero specifically
        try:
            with open("/proc/device-tree/model") as f:
                model = f.read().lower()
                if "zero" in model:
                    return "pi-zero-w"
                elif "raspberry" in model:
                    return "raspberry-pi"
        except (OSError, IOError):
            pass
        return "arm-linux"

    if system == "darwin":
        return "macos"

    return f"{system}-{machine}"


def _get_node_id() -> str:
    """
    Get or generate the node ID.

    Tries to read from config.json first, otherwise generates from MAC.
    """
    config_path = os.environ.get("CONFIG_PATH")
    if config_path:
        try:
            with open(config_path) as f:
                config = json.load(f)
                if "node_id" in config:
                    return config["node_id"]
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # Generate from MAC address
    mac = _get_mac_address().replace(":", "")[-8:]
    return f"jarvis-{mac}"


def _get_capabilities() -> list[str]:
    """Get node capabilities."""
    # Basic capabilities - could be extended with hardware detection
    return ["voice", "speaker"]


def _update_config(room: str, command_center_url: str) -> bool:
    """
    Update the config.json with provisioning data.

    Args:
        room: Room name for the node
        command_center_url: URL of the command center

    Returns:
        True if config was updated successfully
    """
    config_path = os.environ.get("CONFIG_PATH")
    if not config_path:
        return False

    try:
        # Load existing config or create new
        config: dict = {}
        try:
            with open(config_path) as f:
                config = json.load(f)
        except FileNotFoundError:
            pass

        # Update with provisioning data
        config["room"] = room
        config["jarvis_command_center_api_url"] = command_center_url

        # Write back
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)

        return True
    except (OSError, IOError, json.JSONDecodeError):
        return False


def create_provisioning_app(wifi_manager: WiFiManager) -> FastAPI:
    """
    Create the FastAPI provisioning application.

    Args:
        wifi_manager: WiFi manager implementation (real or simulated)

    Returns:
        FastAPI application instance
    """
    app = FastAPI(
        title="Jarvis Node Provisioning",
        description="API for provisioning headless Jarvis nodes",
        version=FIRMWARE_VERSION
    )

    state_machine = ProvisioningStateMachine()
    _provisioning_lock = threading.Lock()

    @app.get("/api/v1/info", response_model=NodeInfo)
    async def get_info() -> NodeInfo:
        """Get node information and current state."""
        return NodeInfo(
            node_id=_get_node_id(),
            firmware_version=FIRMWARE_VERSION,
            hardware=_get_hardware_type(),
            mac_address=_get_mac_address(),
            capabilities=_get_capabilities(),
            state=state_machine.state
        )

    @app.get("/api/v1/scan-networks", response_model=ScanNetworksResponse)
    async def scan_networks() -> ScanNetworksResponse:
        """Scan for available WiFi networks."""
        networks = wifi_manager.scan_networks()
        return ScanNetworksResponse(networks=networks)

    @app.post("/api/v1/provision", response_model=ProvisionResponse)
    async def provision(
        request: ProvisionRequest,
        background_tasks: BackgroundTasks
    ) -> ProvisionResponse:
        """
        Receive WiFi credentials and begin provisioning.

        This endpoint accepts credentials and returns immediately.
        The actual provisioning happens in the background.
        Poll GET /api/v1/status for progress.
        """
        # Only allow one provisioning at a time
        if not _provisioning_lock.acquire(blocking=False):
            return ProvisionResponse(
                success=False,
                message="Provisioning already in progress"
            )

        def do_provisioning() -> None:
            try:
                _run_provisioning(
                    wifi_manager=wifi_manager,
                    state_machine=state_machine,
                    ssid=request.wifi_ssid,
                    password=request.wifi_password,
                    room=request.room,
                    command_center_url=request.command_center_url
                )
            finally:
                _provisioning_lock.release()

        background_tasks.add_task(do_provisioning)

        return ProvisionResponse(
            success=True,
            message="Credentials received. Attempting connection..."
        )

    @app.get("/api/v1/status", response_model=ProvisionStatus)
    async def get_status() -> ProvisionStatus:
        """Get current provisioning status."""
        status = state_machine.get_status()
        return ProvisionStatus(**status)

    return app


def _run_provisioning(
    wifi_manager: WiFiManager,
    state_machine: ProvisioningStateMachine,
    ssid: str,
    password: str,
    room: str,
    command_center_url: str
) -> None:
    """
    Run the full provisioning flow.

    This runs in a background thread.
    """
    try:
        # Step 1: Save credentials
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            f"Saving credentials for {ssid}...",
            progress=10
        )
        save_wifi_credentials(ssid, password)

        # Step 2: Connect to WiFi
        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            f"Connecting to {ssid}...",
            progress=30
        )

        # Stop AP mode if running (real Pi only)
        wifi_manager.stop_ap_mode()

        if not wifi_manager.connect(ssid, password):
            state_machine.set_error(f"Failed to connect to {ssid}")
            return

        state_machine.transition_to(
            ProvisioningState.CONNECTING,
            f"Connected to {ssid}",
            progress=50
        )

        # Step 3: Update config
        state_machine.transition_to(
            ProvisioningState.REGISTERING,
            "Updating configuration...",
            progress=60
        )

        if not _update_config(room, command_center_url):
            state_machine.set_error("Failed to update configuration")
            return

        # Step 4: Register with command center
        state_machine.transition_to(
            ProvisioningState.REGISTERING,
            "Registering with command center...",
            progress=70
        )

        node_id = _get_node_id()

        # Get API key from config if available
        config_path = os.environ.get("CONFIG_PATH")
        api_key = ""
        if config_path:
            try:
                with open(config_path) as f:
                    config = json.load(f)
                    api_key = config.get("api_key", "")
            except (FileNotFoundError, json.JSONDecodeError):
                pass

        if api_key:
            if not register_with_command_center(
                command_center_url=command_center_url,
                node_id=node_id,
                room=room,
                api_key=api_key
            ):
                # Registration failure is not fatal - command center may not have
                # the admin endpoint, or node may already be registered
                pass

        state_machine.transition_to(
            ProvisioningState.REGISTERING,
            "Finalizing...",
            progress=90
        )

        # Step 5: Mark as provisioned
        mark_provisioned()

        state_machine.transition_to(
            ProvisioningState.PROVISIONED,
            "Provisioning complete! You can now start the main Jarvis service.",
            progress=100
        )

    except Exception as e:
        state_machine.set_error(str(e))
