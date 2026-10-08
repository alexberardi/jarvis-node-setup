"""
WiFi management interface and implementations.

Provides a Protocol for WiFi operations with:
- NetworkManagerWiFi: Real implementation using nmcli (Linux/Pi)
- HostapdWiFiManager: Direct hostapd/dnsmasq control for Pi Zero AP mode
- SimulatedWiFi: Simulated implementation for testing

Network Caching:
When in AP mode, the WiFi adapter cannot scan for networks. To support this,
managers should scan networks BEFORE entering AP mode and cache the results.
Use scan_and_cache() before start_ap_mode(), then scan_networks() returns cached data.
"""

import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Protocol

from jarvis_log_client import JarvisLogger

from provisioning.models import NetworkInfo
from utils.encryption_utils import get_secret_dir

logger = JarvisLogger(service="jarvis-node")


def _priv(cmd: list[str]) -> list[str]:
    """Prefix `cmd` with ``sudo -n`` unless we're already root.

    AP-mode operations (systemctl stop|start, hostapd, dnsmasq, ip,
    pkill/killall of root-owned processes) need elevated privileges.
    Post-migration the service runs as ``pi``, so we go through sudo —
    /etc/sudoers.d/jarvis-node (installed by install.sh) grants
    NOPASSWD for the specific binaries this manager uses. ``-n`` makes
    sudo fail fast if NOPASSWD isn't set, instead of stalling on a
    password prompt that nobody can answer.
    """
    if os.geteuid() == 0:
        return cmd
    return ["sudo", "-n", *cmd]


# Global network cache for sharing between manager instances
_cached_networks: list[NetworkInfo] = []
_cache_populated: bool = False


def get_cached_networks() -> list[NetworkInfo]:
    """Get cached networks (may be empty if not populated)."""
    return _cached_networks.copy()


def set_cached_networks(networks: list[NetworkInfo]) -> None:
    """Set the network cache."""
    global _cached_networks, _cache_populated
    _cached_networks = networks.copy()
    _cache_populated = True


def is_cache_populated() -> bool:
    """Check if network cache has been populated."""
    return _cache_populated


def clear_network_cache() -> None:
    """Clear the network cache."""
    global _cached_networks, _cache_populated
    _cached_networks = []
    _cache_populated = False


# --- nmcli profile connect (shared by the nmcli-backed managers) -----------
#
# The WiFi PSK must NEVER appear in an argv. ``sudo`` logs every command line
# to the journal (``COMMAND=/usr/bin/nmcli ... password <psk>``) and any user
# can read argv from /proc while the process runs. So the profile is created
# without a secret and the PSK is handed to ``nmcli connection up`` through a
# 0600 ``passwd-file``. nmcli answers NetworkManager's secret request from
# that file and, because the psk is system-owned (psk-flags 0), NM persists it
# into the root-only keyfile — so autoconnect after reboot keeps working.

_PSK_FILE_PREFIX = ".nm-psk-"
_PROFILE_PREFIX = "jarvis-"
_STAGED_SUFFIX = "-new"


def _profile_name_for(ssid: str) -> str:
    return f"{_PROFILE_PREFIX}{ssid[:20]}"


def _key_mgmt_for(ssid: str) -> str:
    """WPA3-only (SAE) networks need key-mgmt ``sae``; everything else ``wpa-psk``.

    Uses the pre-AP scan cache (the only scan available while in AP mode).
    Mixed WPA2/WPA3 networks accept wpa-psk.
    """
    for net in get_cached_networks():
        if net.ssid != ssid:
            continue
        sec = net.security.upper()
        if "WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec:
            return "sae"
    return "wpa-psk"


def _psk_file_dir() -> Path:
    """Directory for the transient passwd-file: the node's 0700 secret dir."""
    d = get_secret_dir()
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    return d


def _write_psk_file(password: str) -> Path:
    """Write an nmcli passwd-file (0600) holding the PSK; caller must delete it."""
    d = _psk_file_dir()
    # Sweep files a crashed earlier attempt may have left behind.
    for stale in d.glob(f"{_PSK_FILE_PREFIX}*"):
        try:
            stale.unlink()
        except OSError:
            pass
    fd, path = tempfile.mkstemp(prefix=_PSK_FILE_PREFIX, dir=str(d))
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, f"802-11-wireless-security.psk:{password}\n".encode())
    finally:
        os.close(fd)
    return Path(path)


class _NmcliProfileConnector:
    """Connect via an explicit nmcli profile, keeping the PSK off every argv.

    Tracks what the last attempt created so a failed provisioning attempt can
    be rolled back (``rollback_connection``) without touching a profile that
    existed before, and a successful one can be finalised
    (``commit_connection``).
    """

    _interface: str
    _use_sudo: bool = False

    def _nm(self, args: list[str]) -> list[str]:
        cmd = ["nmcli", *args]
        return _priv(cmd) if self._use_sudo else cmd

    def _run_nm(
        self, args: list[str], timeout: float
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            self._nm(args), capture_output=True, text=True, timeout=timeout
        )

    def _profile_exists(self, name: str) -> bool:
        try:
            return self._run_nm(["connection", "show", "id", name], 10).returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _delete_profile(self, name: str) -> bool:
        try:
            return self._run_nm(["connection", "delete", "id", name], 10).returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False

    def _connect_with_profile(self, ssid: str, password: str) -> bool:
        """Create (or stage) a profile for ``ssid`` and bring it up.

        The profile is created with autoconnect off so NetworkManager can't
        race our explicit ``up`` with a secret-less auto-activation;
        ``commit_connection`` turns autoconnect on once registration worked.
        """
        self._attempt_profile = None
        self._replaces_profile = None

        target = _profile_name_for(ssid)
        name = target
        if self._profile_exists(target):
            # Keep the pre-existing profile intact until registration
            # succeeds: stage the new one beside it.
            name = f"{target}{_STAGED_SUFFIX}"
            self._replaces_profile = target
            # A stale staged profile can only be a leftover of an earlier
            # failed attempt — never user data.
            self._delete_profile(name)

        add_args = [
            "connection", "add",
            "type", "wifi",
            "con-name", name,
            "ifname", self._interface,
            "ssid", ssid,
            "connection.autoconnect", "no",
        ]
        if password:
            # No wifi-sec.psk here: the secret arrives via passwd-file below.
            add_args += [
                "wifi-sec.key-mgmt", _key_mgmt_for(ssid),
                "wifi-sec.psk-flags", "0",
            ]

        try:
            result = self._run_nm(add_args, 15)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False
        if result.returncode != 0:
            logger.warning("nmcli connection add failed", stderr=result.stderr.strip())
            return False
        self._attempt_profile = name

        psk_file: Optional[Path] = None
        try:
            up_args = ["connection", "up", "id", name]
            if password:
                psk_file = _write_psk_file(password)
                up_args += ["passwd-file", str(psk_file)]
            result = self._run_nm(up_args, 45)
            if result.returncode == 0:
                return True
            logger.warning("nmcli connection up failed", stderr=result.stderr.strip())
            return False
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("nmcli connection up errored", error=str(e))
            return False
        finally:
            if psk_file is not None:
                try:
                    psk_file.unlink()
                except FileNotFoundError:
                    pass

    def rollback_connection(self) -> None:
        name = getattr(self, "_attempt_profile", None)
        if not name:
            return
        if self._delete_profile(name):
            logger.info("Removed WiFi profile from failed provisioning attempt", profile=name)
        else:
            logger.warning("Could not remove WiFi profile from failed attempt", profile=name)
        self._attempt_profile = None
        self._replaces_profile = None

    def commit_connection(self) -> None:
        name = getattr(self, "_attempt_profile", None)
        old = getattr(self, "_replaces_profile", None)
        if name:
            # Priority 999 matches what install.sh pins on the active profile:
            # the network the user just provisioned must win autoconnect.
            modify = [
                "connection", "modify", "id", name,
                "connection.autoconnect", "yes",
                "connection.autoconnect-priority", "999",
            ]
            if old:
                self._delete_profile(old)
                modify += ["connection.id", old]
            try:
                result = self._run_nm(modify, 10)
                if result.returncode != 0:
                    logger.warning("Could not finalise WiFi profile",
                                   profile=name, stderr=result.stderr.strip())
            except (subprocess.TimeoutExpired, FileNotFoundError):
                logger.warning("Could not finalise WiFi profile", profile=name)
        self._attempt_profile = None
        self._replaces_profile = None


class WiFiManager(Protocol):
    """Protocol for WiFi management operations."""

    def scan_networks(self) -> list[NetworkInfo]:
        """
        Scan for available WiFi networks.

        When in AP mode, returns cached networks if available.
        """
        ...

    def scan_and_cache(self) -> list[NetworkInfo]:
        """
        Scan for networks and cache the results.

        Call this BEFORE entering AP mode to ensure networks are available
        when the mobile app requests them.

        Returns:
            List of discovered networks (also cached for later retrieval)
        """
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

    def rollback_connection(self) -> None:
        """
        Undo the WiFi profile created by the most recent ``connect``.

        Deletes the profile only if that attempt created it; a profile that
        existed before the attempt is left untouched. Called when the
        provisioning attempt fails after (or during) the WiFi join, so a
        retry starts clean and the node's previous WiFi still works.
        """
        ...

    def commit_connection(self) -> None:
        """
        Make the most recent ``connect`` attempt's profile the permanent one.

        Called after registration succeeds. When the attempt had to stage its
        profile next to a pre-existing one, the old profile is replaced.
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


class NetworkManagerWiFi(_NmcliProfileConnector):
    """Real WiFi implementation using NetworkManager (nmcli)."""

    def __init__(self, interface: str = "wlan0") -> None:
        self._interface = interface
        self._attempt_profile: Optional[str] = None
        self._replaces_profile: Optional[str] = None

    def _do_scan(self) -> list[NetworkInfo]:
        """Perform actual WiFi scan using nmcli."""
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

    def scan_networks(self) -> list[NetworkInfo]:
        """
        Scan for available WiFi networks.

        Returns cached networks if available (e.g., when in AP mode),
        otherwise performs a live scan.
        """
        # If cache is populated (we're likely in AP mode), return cached
        if is_cache_populated():
            return get_cached_networks()
        return self._do_scan()

    def scan_and_cache(self) -> list[NetworkInfo]:
        """
        Scan for networks and cache the results.

        Call this BEFORE entering AP mode.
        """
        networks = self._do_scan()
        set_cached_networks(networks)
        return networks

    def connect(self, ssid: str, password: str) -> bool:
        """Connect to a WiFi network using an nmcli profile (PSK via passwd-file)."""
        return self._connect_with_profile(ssid, password)

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
            # Bring down known default hotspot name first.
            result = subprocess.run(
                ["nmcli", "connection", "down", "Hotspot"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if result.returncode == 0:
                return True

            # Fallback: identify any active AP-mode connection and bring it down.
            active = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,TYPE,DEVICE", "connection", "show", "--active"],
                capture_output=True,
                text=True,
                timeout=10
            )
            if active.returncode != 0:
                return False

            for line in active.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split(":")
                if len(parts) < 3:
                    continue
                conn_name, conn_type, device = parts[0], parts[1], parts[2]
                if conn_type == "wifi" and device == "wlan0":
                    down = subprocess.run(
                        ["nmcli", "connection", "down", conn_name],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if down.returncode == 0:
                        return True

            return False
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False


class HostapdWiFiManager(_NmcliProfileConnector):
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
        self._dnsmasq_was_running = False
        self._attempt_profile: Optional[str] = None
        self._replaces_profile: Optional[str] = None

    # nmcli profile operations go through sudo -n: NetworkManager's polkit on
    # Trixie rejects `nmcli connection add` ("Insufficient privileges") for
    # the service user. /etc/sudoers.d/jarvis-node grants NOPASSWD nmcli.
    _use_sudo = True

    def _stop_network_services(self) -> None:
        """Stop NetworkManager, wpa_supplicant, and dnsmasq to release the interface."""
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

        # Check if system dnsmasq is running (conflicts with our DHCP server)
        result = subprocess.run(
            ["systemctl", "is-active", "dnsmasq"],
            capture_output=True,
            text=True
        )
        self._dnsmasq_was_running = result.returncode == 0

        # Stop services
        if self._nm_was_running:
            subprocess.run(_priv(["systemctl", "stop", "NetworkManager"]), capture_output=True)
        if self._wpa_was_running:
            subprocess.run(_priv(["systemctl", "stop", "wpa_supplicant"]), capture_output=True)
        if self._dnsmasq_was_running:
            subprocess.run(_priv(["systemctl", "stop", "dnsmasq"]), capture_output=True)

        # Also kill any running wpa_supplicant processes
        subprocess.run(_priv(["pkill", "-9", "wpa_supplicant"]), capture_output=True)

        # Give services time to stop
        time.sleep(1)

    def _restore_network_services(self) -> None:
        """Restore NetworkManager, wpa_supplicant, and dnsmasq after AP mode."""
        logger.info(f"Restoring services: NM={self._nm_was_running}, wpa={self._wpa_was_running}")

        if self._wpa_was_running:
            result = subprocess.run(_priv(["systemctl", "start", "wpa_supplicant"]), capture_output=True)
            logger.info(f"Started wpa_supplicant: rc={result.returncode}")

        if self._nm_was_running:
            result = subprocess.run(_priv(["systemctl", "start", "NetworkManager"]), capture_output=True)
            logger.info(f"Started NetworkManager: rc={result.returncode}")
        else:
            # Always try to start NetworkManager even if we didn't track it
            logger.warning("NM wasn't tracked as running, starting anyway...")
            result = subprocess.run(_priv(["systemctl", "start", "NetworkManager"]), capture_output=True)
            logger.info(f"Started NetworkManager (fallback): rc={result.returncode}")

        if self._dnsmasq_was_running:
            result = subprocess.run(_priv(["systemctl", "start", "dnsmasq"]), capture_output=True)
            logger.info(f"Started dnsmasq: rc={result.returncode}")

        # Give NetworkManager time to reconnect
        time.sleep(2)

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

# Redirect all DNS to ourselves (captive portal mode)
# This makes iOS/Android captive portal detection hit our server
address=/#/{gateway_ip}

log-queries
log-dhcp
"""

    def _do_scan(self) -> list[NetworkInfo]:
        """Perform actual WiFi scan using nmcli."""
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

    def scan_networks(self) -> list[NetworkInfo]:
        """
        Scan for available WiFi networks.

        Returns cached networks if in AP mode, otherwise performs live scan.
        """
        if is_cache_populated():
            return get_cached_networks()
        return self._do_scan()

    def scan_and_cache(self) -> list[NetworkInfo]:
        """
        Scan for networks and cache the results.

        Call this BEFORE entering AP mode.
        """
        networks = self._do_scan()
        set_cached_networks(networks)
        return networks

    def connect(self, ssid: str, password: str) -> bool:
        """Connect to a WiFi network using nmcli.

        After AP mode teardown, NetworkManager needs time to start scanning,
        so wait (up to 60s) for it to see the target SSID first. The PSK is
        never put on a command line — see ``_NmcliProfileConnector``.

        Goes straight to an explicit profile: ``nmcli dev wifi connect``
        fails on Trixie with "key-mgmt: property is missing" and, worse,
        needs the PSK in argv.
        """
        # Stop AP mode first if active
        if self._ap_active:
            self.stop_ap_mode()

        self._wait_for_ssid(ssid, timeout=60)
        return self._connect_with_profile(ssid, password)

    def _wait_for_ssid(self, ssid: str, timeout: float) -> bool:
        """Poll nmcli until ``ssid`` shows up in a scan, or ``timeout`` passes."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                scan_result = subprocess.run(
                    ["nmcli", "-t", "-f", "SSID", "dev", "wifi", "list", "--rescan", "auto"],
                    capture_output=True,
                    text=True,
                    timeout=15
                )
                if ssid in scan_result.stdout:
                    return True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            logger.info(f"Waiting for NetworkManager to find '{ssid}'...")
            time.sleep(3)
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

        try:
            dnsmasq_conf = self._config_dir / "dnsmasq.conf"

            # Stop network services that might be using the interface
            logger.info("Stopping NetworkManager and wpa_supplicant...")
            self._stop_network_services()

            # Create config directory
            self._config_dir.mkdir(parents=True, exist_ok=True)

            # Write config files
            hostapd_conf = self._config_dir / "hostapd.conf"
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
            logger.info(f"Configuring interface {self._interface}...")
            subprocess.run(
                _priv(["ip", "addr", "flush", "dev", self._interface]),
                capture_output=True,
                timeout=10
            )
            subprocess.run(
                _priv(["ip", "addr", "add", f"{self.DEFAULT_AP_IP}/24", "dev", self._interface]),
                capture_output=True,
                timeout=10
            )
            subprocess.run(
                _priv(["ip", "link", "set", self._interface, "up"]),
                capture_output=True,
                timeout=10
            )

            # Start hostapd
            logger.info(f"Starting hostapd with SSID: {ssid}")
            self._hostapd_process = subprocess.Popen(
                _priv(["hostapd", str(hostapd_conf)]),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )

            # Wait a moment for hostapd to initialize
            time.sleep(2)

            # Check if hostapd is still running
            if self._hostapd_process.poll() is not None:
                # hostapd exited - read error output
                _, stderr = self._hostapd_process.communicate()
                logger.error(f"hostapd failed to start: {stderr.decode()}")
                self._restore_network_services()
                return False

            # Start dnsmasq
            # Pre-clean any stale Jarvis AP dnsmasq from previous crashes.
            subprocess.run(
                _priv(["pkill", "-9", "-f", f"dnsmasq.*{self._config_dir}"]),
                capture_output=True
            )
            logger.info("Starting dnsmasq for DHCP...")
            self._dnsmasq_process = subprocess.Popen(
                _priv(["dnsmasq", "-C", str(dnsmasq_conf), "-d"]),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            time.sleep(1)
            if self._dnsmasq_process.poll() is not None:
                _, stderr = self._dnsmasq_process.communicate()
                logger.error(f"dnsmasq failed to start: {stderr.decode()}")
                self.stop_ap_mode()
                return False

            self._ap_active = True
            logger.info(f"AP mode active - SSID: {ssid}, IP: {self.DEFAULT_AP_IP}")
            return True

        except (FileNotFoundError, PermissionError, OSError) as e:
            logger.error(f"AP mode start failed: {e}")
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
        logger.info("Stopping AP mode...")

        try:
            # Terminate hostapd
            if self._hostapd_process:
                logger.info("Terminating hostapd...")
                self._hostapd_process.terminate()
                try:
                    self._hostapd_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._hostapd_process.kill()
                    self._hostapd_process.wait(timeout=2)
                self._hostapd_process = None

            # Also pkill any stray hostapd processes
            subprocess.run(_priv(["pkill", "-9", "hostapd"]), capture_output=True)

            # Terminate dnsmasq
            if self._dnsmasq_process:
                logger.info("Terminating dnsmasq...")
                self._dnsmasq_process.terminate()
                try:
                    self._dnsmasq_process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self._dnsmasq_process.kill()
                    self._dnsmasq_process.wait(timeout=2)
                self._dnsmasq_process = None

            # Also pkill any stray dnsmasq processes we started.
            subprocess.run(
                _priv(["pkill", "-9", "-f", f"dnsmasq.*{self._config_dir}"]),
                capture_output=True
            )

            # Remove IP from interface
            subprocess.run(
                _priv(["ip", "addr", "flush", "dev", self._interface]),
                capture_output=True,
                timeout=10
            )

            # Explicitly delete the AP route (flush doesn't always remove it)
            subprocess.run(
                _priv(["ip", "route", "del", "192.168.4.0/24"]),
                capture_output=True,
                timeout=10
            )

            # Verify hostapd is actually dead. pgrep is read-only and
            # works without sudo; the killall fallback needs it.
            result = subprocess.run(["pgrep", "hostapd"], capture_output=True)
            if result.returncode == 0:
                logger.warning("hostapd still running, force killing...")
                subprocess.run(_priv(["killall", "-9", "hostapd"]), capture_output=True)
                time.sleep(1)

            self._ap_active = False

            # Restore network services
            logger.info("Restoring network services...")
            self._restore_network_services()

            logger.info("AP mode stopped")
            return True

        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.error(f"Error during AP mode stop: {e}")
            self._ap_active = False
            # Still try to restore network services
            try:
                self._restore_network_services()
            except Exception as e:
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

    def scan_and_cache(self) -> list[NetworkInfo]:
        """Scan and cache networks (simulation just returns the list)."""
        set_cached_networks(self._simulated_networks)
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

    def rollback_connection(self) -> None:
        """Simulate removing the profile from a failed attempt."""
        self._connected_ssid = None

    def commit_connection(self) -> None:
        """Nothing to finalise in simulation."""

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
