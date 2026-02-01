"""
WiFi management interface and implementations.

Provides a Protocol for WiFi operations with:
- NetworkManagerWiFi: Real implementation using nmcli (Linux/Pi)
- HostapdWiFiManager: Direct hostapd/dnsmasq control for Pi Zero AP mode
- SimulatedWiFi: Simulated implementation for testing
"""

import os
import subprocess
from pathlib import Path
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


class HostapdWiFiManager:
    """
    WiFi manager using hostapd and dnsmasq for AP mode.

    This provides more reliable AP mode on Pi Zero compared to NetworkManager's
    hotspot feature. Uses:
    - hostapd: Creates the WiFi access point
    - dnsmasq: Provides DHCP for connecting devices
    - ip: Configures the network interface

    IMPORTANT: Before starting AP mode, this manager stops NetworkManager and
    wpa_supplicant to avoid conflicts. These are restored when AP mode stops.

    For scan/connect operations, delegates to nmcli (same as NetworkManagerWiFi).
    """

    # Default configuration
    DEFAULT_INTERFACE = "wlan0"
    DEFAULT_CHANNEL = 6
    DEFAULT_AP_IP = "192.168.4.1"
    DEFAULT_DHCP_START = "192.168.4.10"
    DEFAULT_DHCP_END = "192.168.4.50"
    DEFAULT_NETMASK = "255.255.255.0"

    def __init__(
        self,
        interface: str = DEFAULT_INTERFACE,
        config_dir: Optional[Path] = None
    ) -> None:
        self._interface = interface
        self._config_dir = config_dir or Path("/tmp/jarvis-ap")
        self._hostapd_process: Optional[subprocess.Popen] = None
        self._dnsmasq_process: Optional[subprocess.Popen] = None
        self._ap_active = False
        self._nm_was_running = False
        self._wpa_was_running = False

    def _stop_network_services(self) -> None:
        """Stop NetworkManager and wpa_supplicant to release the interface."""
        # Check if NetworkManager is running
        result = subprocess.run(
            ["systemctl", "is-active", "NetworkManager"],
            capture_output=True,
            text=True
        )
        self._nm_was_running = result.returncode == 0

        # Check if wpa_supplicant is running
        result = subprocess.run(
            ["systemctl", "is-active", "wpa_supplicant"],
            capture_output=True,
            text=True
        )
        self._wpa_was_running = result.returncode == 0

        # Stop services
        if self._nm_was_running:
            subprocess.run(["systemctl", "stop", "NetworkManager"], capture_output=True)
        if self._wpa_was_running:
            subprocess.run(["systemctl", "stop", "wpa_supplicant"], capture_output=True)

        # Also kill any running wpa_supplicant processes
        subprocess.run(["pkill", "-9", "wpa_supplicant"], capture_output=True)

        # Give services time to stop
        import time
        time.sleep(1)

    def _restore_network_services(self) -> None:
        """Restore NetworkManager and wpa_supplicant after AP mode."""
        if self._wpa_was_running:
            subprocess.run(["systemctl", "start", "wpa_supplicant"], capture_output=True)
        if self._nm_was_running:
            subprocess.run(["systemctl", "start", "NetworkManager"], capture_output=True)

    def _generate_hostapd_config(self, ssid: str, interface: str, channel: int) -> str:
        """Generate hostapd configuration file content."""
        return f"""# Jarvis AP Mode - hostapd configuration
interface={interface}
driver=nl80211
ssid={ssid}
hw_mode=g
channel={channel}
wmm_enabled=0
macaddr_acl=0
auth_algs=1
ignore_broadcast_ssid=0
wpa=0
"""

    def _generate_dnsmasq_config(
        self,
        interface: str,
        gateway_ip: str,
        dhcp_start: str,
        dhcp_end: str
    ) -> str:
        """Generate dnsmasq configuration file content."""
        return f"""# Jarvis AP Mode - dnsmasq configuration
interface={interface}
bind-interfaces
dhcp-range={dhcp_start},{dhcp_end},12h
dhcp-option=3,{gateway_ip}
dhcp-option=6,{gateway_ip}
server=8.8.8.8
log-queries
log-dhcp
"""

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
                        signal_dbm = -90 + int(signal * 0.6)
                    except (ValueError, IndexError):
                        signal_dbm = -70

                    security = parts[2].strip() if len(parts) > 2 else "OPEN"

                    networks.append(NetworkInfo(
                        ssid=ssid,
                        signal_strength=signal_dbm,
                        security=security
                    ))

            networks.sort(key=lambda n: n.signal_strength, reverse=True)
            return networks

        except (subprocess.TimeoutExpired, FileNotFoundError):
            return []

    def connect(self, ssid: str, password: str) -> bool:
        """Connect to a WiFi network using nmcli."""
        # Stop AP mode first if active
        if self._ap_active:
            self.stop_ap_mode()

        try:
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
        Start AP mode using hostapd and dnsmasq.

        Steps:
        1. Stop NetworkManager/wpa_supplicant to release interface
        2. Create config directory
        3. Write hostapd.conf and dnsmasq.conf
        4. Assign IP to interface
        5. Start hostapd process
        6. Start dnsmasq process
        """
        import time

        try:
            # Stop network services that might be using the interface
            print("[hostapd] Stopping NetworkManager and wpa_supplicant...")
            self._stop_network_services()

            # Create config directory
            self._config_dir.mkdir(parents=True, exist_ok=True)

            # Write config files
            hostapd_conf = self._config_dir / "hostapd.conf"
            dnsmasq_conf = self._config_dir / "dnsmasq.conf"

            hostapd_conf.write_text(
                self._generate_hostapd_config(ssid, self._interface, self.DEFAULT_CHANNEL)
            )
            dnsmasq_conf.write_text(
                self._generate_dnsmasq_config(
                    self._interface,
                    self.DEFAULT_AP_IP,
                    self.DEFAULT_DHCP_START,
                    self.DEFAULT_DHCP_END
                )
            )

            # Flush existing IP and assign new one
            print(f"[hostapd] Configuring interface {self._interface}...")
            subprocess.run(
                ["ip", "addr", "flush", "dev", self._interface],
                capture_output=True,
                timeout=10
            )
            subprocess.run(
                ["ip", "addr", "add", f"{self.DEFAULT_AP_IP}/24", "dev", self._interface],
                capture_output=True,
                timeout=10
            )
            subprocess.run(
                ["ip", "link", "set", self._interface, "up"],
                capture_output=True,
                timeout=10
            )

            # Start hostapd
            print(f"[hostapd] Starting hostapd with SSID: {ssid}")
            self._hostapd_process = subprocess.Popen(
                ["hostapd", str(hostapd_conf)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Wait a moment for hostapd to initialize
            time.sleep(2)

            # Check if hostapd is still running
            if self._hostapd_process.poll() is not None:
                # hostapd exited - read error output
                _, stderr = self._hostapd_process.communicate()
                print(f"[hostapd] ERROR: hostapd failed to start: {stderr.decode()}")
                self._restore_network_services()
                return False

            # Start dnsmasq
            print("[hostapd] Starting dnsmasq for DHCP...")
            self._dnsmasq_process = subprocess.Popen(
                ["dnsmasq", "-C", str(dnsmasq_conf), "-d"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            self._ap_active = True
            print(f"[hostapd] ✅ AP mode active - SSID: {ssid}, IP: {self.DEFAULT_AP_IP}")
            return True

        except (FileNotFoundError, PermissionError, OSError) as e:
            print(f"[hostapd] ERROR: {e}")
            # Clean up on failure
            self.stop_ap_mode()
            return False

    def stop_ap_mode(self) -> bool:
        """
        Stop AP mode by terminating hostapd and dnsmasq.

        Steps:
        1. Terminate hostapd process
        2. Terminate dnsmasq process
        3. Remove IP from interface
        4. Restore NetworkManager/wpa_supplicant
        """
        print("[hostapd] Stopping AP mode...")

        try:
            # Terminate hostapd
            if self._hostapd_process:
                print("[hostapd] Terminating hostapd...")
                self._hostapd_process.terminate()
                try:
                    self._hostapd_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._hostapd_process.kill()
                    self._hostapd_process.wait(timeout=2)
                self._hostapd_process = None

            # Also pkill any stray hostapd processes
            subprocess.run(["pkill", "-9", "hostapd"], capture_output=True)

            # Terminate dnsmasq
            if self._dnsmasq_process:
                print("[hostapd] Terminating dnsmasq...")
                self._dnsmasq_process.terminate()
                try:
                    self._dnsmasq_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._dnsmasq_process.kill()
                    self._dnsmasq_process.wait(timeout=2)
                self._dnsmasq_process = None

            # Also pkill any stray dnsmasq processes we started
            subprocess.run(["pkill", "-9", "-f", "dnsmasq.*jarvis-ap"], capture_output=True)

            # Remove IP from interface
            subprocess.run(
                ["ip", "addr", "flush", "dev", self._interface],
                capture_output=True,
                timeout=10
            )

            self._ap_active = False

            # Restore network services
            print("[hostapd] Restoring network services...")
            self._restore_network_services()

            print("[hostapd] ✅ AP mode stopped")
            return True

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            print(f"[hostapd] Error during stop: {e}")
            self._ap_active = False
            # Still try to restore network services
            try:
                self._restore_network_services()
            except Exception:
                pass
            return True  # Best effort - still return True


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

    Environment variables:
    - JARVIS_SIMULATE_PROVISIONING=true: Returns SimulatedWiFi
    - JARVIS_WIFI_BACKEND=hostapd: Returns HostapdWiFiManager
    - Otherwise: Returns NetworkManagerWiFi (default)
    """
    simulate = os.environ.get("JARVIS_SIMULATE_PROVISIONING", "false").lower()
    if simulate in ("true", "1", "yes"):
        return SimulatedWiFi()

    backend = os.environ.get("JARVIS_WIFI_BACKEND", "").lower()
    if backend == "hostapd":
        return HostapdWiFiManager()

    return NetworkManagerWiFi()
