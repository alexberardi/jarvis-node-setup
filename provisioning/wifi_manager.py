"""
WiFi management interface and implementations.

Provides a Protocol for WiFi operations with:
- NetworkManagerWiFi: Real implementation using nmcli (Linux/Pi)
- SimulatedWiFi: Simulated implementation for testing
"""

import os
import subprocess
from typing import Optional, Protocol

from provisioning.models import NetworkInfo


class WiFiManager(Protocol):
    """Protocol for WiFi management operations."""

    def scan_networks(self) -> list[NetworkInfo]:
        """Scan for available WiFi networks."""
        ...

    def connect(self, ssid: str, password: str) -> bool:
        """
        Connect to a WiFi network.

        Args:
            ssid: Network SSID
            password: Network password

        Returns:
            True if connection successful, False otherwise
        """
        ...

    def get_current_ssid(self) -> Optional[str]:
        """Get the SSID of the currently connected network, if any."""
        ...

    def start_ap_mode(self, ssid: str) -> bool:
        """
        Start AP mode for provisioning.

        Args:
            ssid: SSID to broadcast

        Returns:
            True if AP mode started successfully
        """
        ...

    def stop_ap_mode(self) -> bool:
        """
        Stop AP mode.

        Returns:
            True if AP mode stopped successfully
        """
        ...


class NetworkManagerWiFi:
    """Real WiFi implementation using NetworkManager (nmcli)."""

    def scan_networks(self) -> list[NetworkInfo]:
        """Scan for available WiFi networks using nmcli."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list"],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode != 0:
                return []

            networks: list[NetworkInfo] = []
            seen_ssids: set[str] = set()

            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split(":")
                if len(parts) >= 3:
                    ssid = parts[0].strip()
                    if not ssid or ssid in seen_ssids:
                        continue
                    seen_ssids.add(ssid)

                    try:
                        signal = int(parts[1])
                        # Convert percentage to approximate dBm
                        # 100% ≈ -30dBm, 0% ≈ -90dBm
                        signal_dbm = -90 + int(signal * 0.6)
                    except (ValueError, IndexError):
                        signal_dbm = -70

                    security = parts[2].strip() if len(parts) > 2 else "OPEN"

                    networks.append(NetworkInfo(
                        ssid=ssid,
                        signal_strength=signal_dbm,
                        security=security
                    ))

            # Sort by signal strength (strongest first)
            networks.sort(key=lambda n: n.signal_strength, reverse=True)
            return networks

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def connect(self, ssid: str, password: str) -> bool:
        """Connect to a WiFi network using nmcli."""
        try:
            # First, try to connect using existing connection profile
            result = subprocess.run(
                ["nmcli", "dev", "wifi", "connect", ssid, "password", password],
                capture_output=True,
                text=True,
                timeout=60
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def get_current_ssid(self) -> Optional[str]:
        """Get the current connected WiFi SSID."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                return None

            for line in result.stdout.strip().split("\n"):
                if line.startswith("yes:"):
                    return line.split(":", 1)[1]

            return None
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return None

    def start_ap_mode(self, ssid: str) -> bool:
        """
        Start AP mode using NetworkManager hotspot.

        Note: This requires proper NetworkManager configuration and may need
        root privileges on some systems.
        """
        try:
            # Create a hotspot connection
            result = subprocess.run(
                [
                    "nmcli", "dev", "wifi", "hotspot",
                    "ifname", "wlan0",
                    "ssid", ssid,
                    "password", "jarvis-setup"  # Simple password for setup
                ],
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def stop_ap_mode(self) -> bool:
        """Stop AP mode by deactivating the hotspot connection."""
        try:
            # Find and deactivate the hotspot connection
            result = subprocess.run(
                ["nmcli", "connection", "down", "Hotspot"],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False


class SimulatedWiFi:
    """Simulated WiFi for testing without real hardware."""

    def __init__(self) -> None:
        self._connected_ssid: Optional[str] = None
        self._ap_mode_active: bool = False
        self._simulated_networks: list[NetworkInfo] = [
            NetworkInfo(ssid="HomeNetwork", signal_strength=-45, security="WPA2"),
            NetworkInfo(ssid="Neighbor_5G", signal_strength=-72, security="WPA2"),
            NetworkInfo(ssid="CoffeeShop_Free", signal_strength=-80, security="OPEN"),
            NetworkInfo(ssid="IoT_Network", signal_strength=-55, security="WPA3"),
        ]

    def scan_networks(self) -> list[NetworkInfo]:
        """Return simulated network list."""
        return self._simulated_networks

    def connect(self, ssid: str, password: str) -> bool:
        """
        Simulate WiFi connection.

        Always succeeds for known networks (those in the simulated list).
        """
        known_ssids = {n.ssid for n in self._simulated_networks}
        if ssid in known_ssids:
            self._connected_ssid = ssid
            self._ap_mode_active = False
            return True
        return False

    def get_current_ssid(self) -> Optional[str]:
        """Return the simulated connected SSID."""
        return self._connected_ssid

    def start_ap_mode(self, ssid: str) -> bool:
        """Simulate starting AP mode."""
        self._ap_mode_active = True
        self._connected_ssid = None
        return True

    def stop_ap_mode(self) -> bool:
        """Simulate stopping AP mode."""
        self._ap_mode_active = False
        return True


def get_wifi_manager() -> WiFiManager:
    """
    Get the appropriate WiFi manager based on environment.

    Returns SimulatedWiFi if JARVIS_SIMULATE_PROVISIONING=true,
    otherwise returns NetworkManagerWiFi.
    """
    simulate = os.environ.get("JARVIS_SIMULATE_PROVISIONING", "false").lower()
    if simulate in ("true", "1", "yes"):
        return SimulatedWiFi()
    return NetworkManagerWiFi()
