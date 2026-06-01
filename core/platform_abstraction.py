"""
Platform abstraction layer for Jarvis Node.

This module provides platform-agnostic interfaces and platform-specific implementations
using dependency injection and composition patterns.
"""

import os
import platform
import re
import threading
import time as _time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Any
import subprocess

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


class AudioProvider(ABC):
    """Abstract interface for audio operations"""

    def __init__(self):
        self._playback_lock = threading.Lock()
        self._playback_proc: subprocess.Popen | None = None
        self._cancel_event = threading.Event()
        # Closeable handles (typically the in-flight streaming
        # requests.Response) registered by callers that own
        # cancellable I/O. cancel_playback closes them so any
        # blocked iter_content / socket read raises promptly,
        # rather than waiting for the next chunk that the
        # subprocess kill alone doesn't preempt.
        # Duck-typed: anything with a no-arg .close() method works.
        self._cancel_closeables: list[Any] = []

    def register_cancel_closeable(self, closeable: Any) -> None:
        """Register an object whose ``close()`` should fire on cancel.

        The caller MUST pair this with ``unregister_cancel_closeable``
        in a ``finally`` block — otherwise stale handles accumulate and
        the next cancel tries to close already-dead objects (harmless
        but wasteful).
        """
        with self._playback_lock:
            self._cancel_closeables.append(closeable)

    def unregister_cancel_closeable(self, closeable: Any) -> None:
        """Remove a previously-registered closeable. Idempotent."""
        with self._playback_lock:
            try:
                self._cancel_closeables.remove(closeable)
            except ValueError:
                pass

    def cancel_playback(self) -> bool:
        """Cancel any active audio playback (barge-in support).

        Sets the cancel event (checked by playback loops and used to
        pre-empt future playback calls), closes any registered
        cancellable streaming handles so blocked iter_content unblocks,
        and kills the active subprocess. Thread-safe — can be called
        from any thread.

        Returns True if a process was cancelled.
        """
        self._cancel_event.set()
        # Snapshot the closeables list inside the lock, then close
        # outside — close() can be slow (TLS teardown) and we don't
        # want to hold the lock during that.
        with self._playback_lock:
            to_close = list(self._cancel_closeables)
        if to_close:
            logger.info(
                "Closing registered cancel closeables (barge-in)",
                count=len(to_close),
            )
        for closeable in to_close:
            try:
                closeable.close()
            except Exception as e:
                logger.debug("Cancel closeable.close() raised", error=str(e))
            # requests.Response.close() only releases the connection back
            # to the pool — it doesn't reliably interrupt a blocked
            # iter_content read in another thread. Reach into the raw
            # urllib3 response and close the underlying file pointer too;
            # that forces a socket.recv() in progress to raise instead of
            # waiting for the next chunk that may never come. Best-effort,
            # silently tolerates anything that isn't a requests.Response.
            raw = getattr(closeable, "raw", None)
            if raw is not None:
                try:
                    raw.close()
                except Exception:
                    pass
                fp = getattr(raw, "_fp", None)
                if fp is not None:
                    try:
                        fp.close()
                    except Exception:
                        pass
        with self._playback_lock:
            proc = self._playback_proc
            if proc is None:
                # Even with no aplay running, closing the upstream
                # response is the whole point of the call — report
                # success when at least one closeable fired.
                return bool(to_close)
            try:
                proc.kill()
                logger.info("Cancelled active audio playback (barge-in)")
                return True
            except OSError:
                return False

    def reset_cancel(self) -> None:
        """Clear the cancel event so future playback proceeds normally.

        Call this after handling a barge-in before starting a new
        interaction that needs audio playback.
        """
        self._cancel_event.clear()

    @property
    def is_cancelled(self) -> bool:
        """True if playback was cancelled (reset on next playback start)."""
        return self._cancel_event.is_set()

    def _set_playback_proc(self, proc: subprocess.Popen) -> None:
        with self._playback_lock:
            self._playback_proc = proc

    def _clear_playback_proc(self) -> None:
        with self._playback_lock:
            self._playback_proc = None

    @abstractmethod
    def play_audio_file(self, file_path: str, volume: float = 1.0) -> bool:
        """Play an audio file"""
        pass

    @abstractmethod
    def play_chime(self, chime_path: str) -> bool:
        """Play a chime sound"""
        pass

    @abstractmethod
    def get_audio_devices(self) -> List[Dict[str, Any]]:
        """Get available audio devices"""
        pass

    def play_pcm_stream(
        self,
        pcm_iterator,
        sample_rate: int = 22050,
        channels: int = 1,
        sample_width: int = 2,
    ) -> bool:
        """Play raw PCM audio from an iterator of byte chunks.

        Args:
            pcm_iterator: Iterator yielding raw PCM byte chunks
            sample_rate: Audio sample rate in Hz
            channels: Number of audio channels
            sample_width: Sample width in bytes (2 = 16-bit)

        Returns:
            True if playback succeeded
        """
        # Pre-empt: if cancel was requested before playback started
        # (e.g. barge-in during STT/processing), skip entirely.
        if self._cancel_event.is_set():
            logger.info("PCM playback pre-empted (barge-in)")
            return False
        # Pipe raw PCM into aplay over stdin instead of going through PyAudio.
        # PyAudio on Pi Zero 2 W was glitching audibly even when network
        # chunks arrived on time. aplay is the same binary the short-TTS
        # path uses successfully, and it talks directly to the ALSA device
        # we've configured in /etc/asound.conf.
        format_arg = {1: "U8", 2: "S16_LE", 3: "S24_LE", 4: "S32_LE"}.get(
            sample_width, "S16_LE"
        )
        cmd = [
            "aplay",
            "-D", "output",      # the pulse alias defined in /etc/asound.conf
            "-q",
            "-f", format_arg,
            "-r", str(sample_rate),
            "-c", str(channels),
            "-t", "raw",
            "-",
        ]
        logger.info(
            "PCM stream starting aplay",
            sample_rate=sample_rate,
            channels=channels,
            sample_width=sample_width,
            format=format_arg,
        )
        self._cancel_event.clear()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._set_playback_proc(proc)
            start_ts = _time.monotonic()
            total_bytes = 0
            chunk_count = 0
            assert proc.stdin is not None
            try:
                for chunk in pcm_iterator:
                    if not chunk:
                        continue
                    if self._cancel_event.is_set():
                        logger.info("PCM stream cancelled (barge-in)")
                        break
                    chunk_count += 1
                    total_bytes += len(chunk)
                    try:
                        proc.stdin.write(chunk)
                    except BrokenPipeError:
                        logger.warning("aplay pipe closed early")
                        break
            finally:
                self._clear_playback_proc()
                try:
                    proc.stdin.close()
                except BrokenPipeError:
                    pass
                # Wait for aplay to drain the buffer. 30s is way more than
                # any realistic message length.
                try:
                    proc.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    logger.warning("aplay did not drain within 30s, killed")
            stderr_out = (proc.stderr.read() if proc.stderr else b"").decode(
                errors="replace"
            ).strip()
            logger.info(
                "PCM stream complete",
                total_bytes=total_bytes,
                chunks=chunk_count,
                duration_s=round(_time.monotonic() - start_ts, 2),
                returncode=proc.returncode,
                stderr=stderr_out[:200] if stderr_out else None,
            )
            return proc.returncode == 0
        except Exception as e:
            logger.error(f"Error playing PCM stream: {e}")
            return False


class NetworkDiscoveryProvider(ABC):
    """Abstract interface for network discovery operations"""
    
    @abstractmethod
    def discover_mdns_services(self) -> Dict[str, Any]:
        """Discover mDNS services"""
        pass
    
    @abstractmethod
    def get_network_info(self) -> Dict[str, Any]:
        """Get network information"""
        pass
    
    @abstractmethod
    def scan_network_range(self, network_range: str) -> List[str]:
        """Scan a network range for active hosts"""
        pass


class SystemProvider(ABC):
    """Abstract interface for system operations"""
    
    @abstractmethod
    def get_system_info(self) -> Dict[str, Any]:
        """Get system information"""
        pass
    
    @abstractmethod
    def install_package(self, package_name: str) -> bool:
        """Install a system package"""
        pass
    
    @abstractmethod
    def get_audio_config_path(self) -> str:
        """Get the path for audio configuration"""
        pass


# macOS Implementations
class MacOSAudioProvider(AudioProvider):
    """macOS-specific audio provider using afplay"""

    def __init__(self):
        super().__init__()

    def play_audio_file(self, file_path: str, volume: float = 1.0) -> bool:
        if self._cancel_event.is_set():
            logger.info("Audio playback pre-empted (barge-in)")
            return False
        self._cancel_event.clear()
        try:
            proc = subprocess.Popen(
                ["afplay", file_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._set_playback_proc(proc)
            try:
                while proc.poll() is None:
                    if self._cancel_event.is_set():
                        proc.kill()
                        logger.info("afplay cancelled (barge-in)")
                        return False
                    _time.sleep(0.05)
                return proc.returncode == 0
            finally:
                self._clear_playback_proc()
        except Exception as e:
            logger.error(f"Error playing audio file: {e}")
            return False

    def play_chime(self, chime_path: str) -> bool:
        return self.play_audio_file(chime_path)
    
    def get_audio_devices(self) -> List[Dict[str, Any]]:
        try:
            # Use system_profiler to get audio device info
            result = subprocess.run(
                ["system_profiler", "SPAudioDataType"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            devices = []
            if result.returncode == 0:
                # Parse system_profiler output (simplified)
                devices.append({
                    "name": "Default Output",
                    "type": "output",
                    "platform": "macos"
                })
            
            return devices
        except Exception as e:
            logger.error(f"Error getting audio devices: {e}")
            return []


class MacOSNetworkDiscoveryProvider(NetworkDiscoveryProvider):
    """macOS-specific network discovery provider"""
    
    def discover_mdns_services(self) -> Dict[str, Any]:
        services = {}
        
        # Use dns-sd with timeout to avoid hanging
        try:
            result = subprocess.run(
                ["timeout", "8", "dns-sd", "-B", "_services._dns-sd._udp", "local"],
                capture_output=True,
                text=True
            )
            
            if result.returncode == 0:
                services["dns_sd"] = self._parse_dns_sd_output(result.stdout)
        except Exception as e:
            logger.error(f"Error in dns-sd discovery: {e}")
        
        # Use dig for specific services
        dig_services = [
            "_airplay._tcp.local",
            "_googlecast._tcp.local", 
            "_home-assistant._tcp.local",
            "_hue._tcp.local",
            "_spotify-connect._tcp.local"
        ]
        
        for service in dig_services:
            try:
                result = subprocess.run(
                    ["dig", "+short", service, "PTR"],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    services[service] = result.stdout.strip().split('\n')
            except Exception as e:
                logger.error(f"Error querying {service}: {e}")
        
        return services
    
    def _parse_dns_sd_output(self, output: str) -> List[Dict[str, Any]]:
        """Parse dns-sd output into structured data"""
        services = []
        for line in output.strip().split('\n'):
            if 'Add' in line and 'Flags' in line:
                # Parse dns-sd browse output
                parts = line.split()
                if len(parts) >= 6:
                    services.append({
                        "timestamp": parts[0],
                        "action": parts[1],
                        "flags": parts[2],
                        "interface": parts[3],
                        "domain": parts[4],
                        "service_type": parts[5],
                        "instance_name": parts[6] if len(parts) > 6 else ""
                    })
        return services
    
    def get_network_info(self) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                ["scutil", "--dns"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            return {
                "dns_info": result.stdout if result.returncode == 0 else "",
                "platform": "macos"
            }
        except Exception as e:
            return {"error": str(e), "platform": "macos"}
    
    def scan_network_range(self, network_range: str) -> List[str]:
        # Use ping sweep for network scanning on macOS
        active_hosts = []
        try:
            # Simple ping sweep
            base_ip = network_range.split('/')[0]
            base_parts = base_ip.split('.')
            
            for i in range(1, 255):
                ip = f"{base_parts[0]}.{base_parts[1]}.{base_parts[2]}.{i}"
                result = subprocess.run(
                    ["ping", "-c", "1", "-W", "1", ip],
                    capture_output=True,
                    text=True
                )
                if result.returncode == 0:
                    active_hosts.append(ip)
        except Exception as e:
            logger.error(f"Error scanning network: {e}")
        
        return active_hosts


class MacOSSystemProvider(SystemProvider):
    """macOS-specific system provider"""
    
    def get_system_info(self) -> Dict[str, Any]:
        return {
            "platform": "macos",
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor()
        }
    
    def install_package(self, package_name: str) -> bool:
        try:
            result = subprocess.run(
                ["brew", "install", package_name],
                capture_output=True,
                text=True,
                timeout=300
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error installing package {package_name}: {e}")
            return False
    
    def get_audio_config_path(self) -> str:
        return os.path.expanduser("~/.config/jarvis-node/audio_config.json")


# Raspberry Pi/Linux Implementations
class PiAudioProvider(AudioProvider):
    """Raspberry Pi/Linux-specific audio provider using ALSA"""

    def __init__(self):
        super().__init__()

    def play_audio_file(self, file_path: str, volume: float = 1.0) -> bool:
        if self._cancel_event.is_set():
            logger.info("Audio playback pre-empted (barge-in)")
            return False
        self._cancel_event.clear()
        try:
            if volume != 1.0:
                proc = subprocess.Popen(
                    f"sox {file_path} -t wav - vol {volume} | aplay -D output",
                    shell=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            else:
                proc = subprocess.Popen(
                    ["aplay", "-D", "output", file_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
            self._set_playback_proc(proc)
            try:
                while proc.poll() is None:
                    if self._cancel_event.is_set():
                        proc.kill()
                        logger.info("aplay cancelled (barge-in)")
                        return False
                    _time.sleep(0.05)
                return proc.returncode == 0
            finally:
                self._clear_playback_proc()
        except Exception as e:
            logger.error(f"Error playing audio file: {e}")
            return False
    
    def play_chime(self, chime_path: str) -> bool:
        try:
            result = subprocess.run(
                ["aplay", chime_path],
                capture_output=True,
                text=True,
                timeout=10
            )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error playing chime: {e}")
            return False
    
    def get_audio_devices(self) -> List[Dict[str, Any]]:
        try:
            result = subprocess.run(
                ["aplay", "-l"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            devices = []
            if result.returncode == 0:
                # Parse aplay -l output
                for line in result.stdout.split('\n'):
                    if 'card' in line and 'device' in line:
                        devices.append({
                            "name": line.strip(),
                            "type": "output",
                            "platform": "linux"
                        })
            
            return devices
        except Exception as e:
            logger.error(f"Error getting audio devices: {e}")
            return []


class PiNetworkDiscoveryProvider(NetworkDiscoveryProvider):
    """Raspberry Pi/Linux-specific network discovery provider"""
    
    def discover_mdns_services(self) -> Dict[str, Any]:
        services = {}
        
        # Use avahi-browse for comprehensive mDNS discovery
        try:
            result = subprocess.run(
                ["avahi-browse", "-at", "-r"],
                capture_output=True,
                text=True,
                timeout=15
            )
            
            if result.returncode == 0:
                services["avahi"] = self._parse_avahi_output(result.stdout)
        except Exception as e:
            logger.error(f"Error in avahi-browse discovery: {e}")
        
        # Use systemd-resolve for additional discovery
        try:
            result = subprocess.run(
                ["systemd-resolve", "--mdns=yes"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode == 0:
                services["systemd_resolve"] = result.stdout
        except Exception as e:
            logger.error(f"Error in systemd-resolve discovery: {e}")
        
        return services
    
    def _parse_avahi_output(self, output: str) -> List[Dict[str, Any]]:
        """Parse avahi-browse output into structured data"""
        services = []
        for line in output.strip().split('\n'):
            if ';' in line:
                parts = line.split(';')
                if len(parts) >= 4:
                    services.append({
                        "interface": parts[0],
                        "protocol": parts[1],
                        "service_type": parts[2],
                        "service_name": parts[3],
                        "hostname": parts[4] if len(parts) > 4 else "",
                        "address": parts[5] if len(parts) > 5 else "",
                        "port": parts[6] if len(parts) > 6 else "",
                        "txt": parts[7] if len(parts) > 7 else ""
                    })
        return services
    
    def get_network_info(self) -> Dict[str, Any]:
        try:
            result = subprocess.run(
                ["systemd-resolve", "--status"],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            return {
                "dns_info": result.stdout if result.returncode == 0 else "",
                "platform": "linux"
            }
        except Exception as e:
            return {"error": str(e), "platform": "linux"}
    
    def scan_network_range(self, network_range: str) -> List[str]:
        # Use nmap for network scanning on Linux
        active_hosts = []
        try:
            result = subprocess.run(
                ["nmap", "-sn", network_range],
                capture_output=True,
                text=True,
                timeout=60
            )
            
            if result.returncode == 0:
                # Parse nmap output for active hosts
                for line in result.stdout.split('\n'):
                    if 'Nmap scan report for' in line:
                        ip = line.split()[-1].strip('()')
                        active_hosts.append(ip)
        except Exception as e:
            logger.error(f"Error scanning network: {e}")
        
        return active_hosts


class PiSystemProvider(SystemProvider):
    """Raspberry Pi/Linux-specific system provider"""
    
    def get_system_info(self) -> Dict[str, Any]:
        return {
            "platform": "linux",
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor()
        }
    
    def install_package(self, package_name: str) -> bool:
        try:
            result = subprocess.run(
                ["apt-get", "update"],
                capture_output=True,
                text=True,
                timeout=300
            )
            if result.returncode == 0:
                result = subprocess.run(
                    ["apt-get", "install", "-y", package_name],
                    capture_output=True,
                    text=True,
                    timeout=300
                )
            return result.returncode == 0
        except Exception as e:
            logger.error(f"Error installing package {package_name}: {e}")
            return False
    
    def get_audio_config_path(self) -> str:
        return "/etc/jarvis-node/audio_config.json"


# Bluetooth Abstraction
@dataclass
class BluetoothDevice:
    """Represents a discovered or paired Bluetooth device."""
    name: str
    mac_address: str
    device_type: str = "unknown"  # "audio_sink" | "audio_source" | "phone" | "unknown"
    rssi: int | None = None
    paired: bool = False
    connected: bool = False


class BluetoothProvider(ABC):
    """Abstract interface for Bluetooth operations."""

    @abstractmethod
    def is_available(self) -> bool:
        """Check if Bluetooth hardware is available and the adapter is powered on."""
        pass

    @abstractmethod
    def scan(self, timeout: float = 10.0) -> list[BluetoothDevice]:
        """Scan for nearby Bluetooth devices."""
        pass

    @abstractmethod
    def pair(self, mac_address: str) -> bool:
        """Pair with a device by MAC address."""
        pass

    @abstractmethod
    def connect(self, mac_address: str) -> bool:
        """Connect to a paired device."""
        pass

    @abstractmethod
    def disconnect(self, mac_address: str) -> bool:
        """Disconnect from a connected device."""
        pass

    @abstractmethod
    def remove(self, mac_address: str) -> bool:
        """Remove (forget) a paired device."""
        pass

    @abstractmethod
    def set_discoverable(self, enabled: bool, timeout: int = 120) -> bool:
        """Make the adapter discoverable (or not)."""
        pass

    @abstractmethod
    def get_paired_devices(self) -> list[BluetoothDevice]:
        """List all paired devices."""
        pass

    @abstractmethod
    def get_connected_devices(self) -> list[BluetoothDevice]:
        """List currently connected devices."""
        pass

    @abstractmethod
    def trust(self, mac_address: str) -> bool:
        """Trust a device for auto-accept pairing."""
        pass


class PiBluetoothProvider(BluetoothProvider):
    """Raspberry Pi Bluetooth provider using bluetoothctl subprocess."""

    def _run_bluetoothctl(self, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
        """Run a bluetoothctl command and return the result."""
        cmd = ["bluetoothctl"] + list(args)
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def is_available(self) -> bool:
        try:
            result = self._run_bluetoothctl("show", timeout=5.0)
            return result.returncode == 0 and "Powered: yes" in result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.warning("Bluetooth not available", error=str(e))
            return False

    def scan(self, timeout: float = 30.0) -> list[BluetoothDevice]:
        """Scan for nearby Bluetooth devices using a single bluetoothctl session.

        Runs a BR/EDR pass followed by an LE pass within one bluetoothctl
        process so most audio devices (classic) AND BLE-only devices both
        show up. Each pass gets half of `timeout`. Stale (non-paired)
        devices are pruned from BlueZ's cache before scanning so the
        result reflects what's currently in range, not ghosts from days
        ago.

        Uses Popen to keep the process alive during discovery — killing
        the process that started scan causes BlueZ to cancel discovery
        via D-Bus.
        """
        import time

        # Drop ghosts before scanning so we don't merge cached, never-paired
        # devices with the live scan results.
        self._prune_stale_cache()

        # Each mode gets half the budget, with a 10s floor so neither pass
        # is uselessly short on a tight timeout.
        per_mode = max(timeout / 2, 10.0)

        try:
            proc = subprocess.Popen(
                ["bluetoothctl"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

            for mode in ("bredr", "le"):
                proc.stdin.write(f"scan {mode}\n")
                proc.stdin.flush()
                time.sleep(per_mode)
                proc.stdin.write("scan off\n")
                proc.stdin.flush()
                time.sleep(0.5)

            proc.stdin.write("devices\n")
            proc.stdin.flush()
            time.sleep(0.5)

            proc.stdin.write("quit\n")
            proc.stdin.flush()

            stdout, _ = proc.communicate(timeout=5.0)

        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            logger.error("Bluetooth scan failed", error=str(e))
            try:
                proc.kill()
            except Exception:
                pass
            return []

        # Parse the `devices` command listing — NOT the live discovery
        # events. During scan, bluetoothctl prints `[NEW] Device …` for
        # each find and `[CHG] Device <MAC> RSSI: -52` for property
        # updates; matching those leaks "RSSI: …" or "ManufacturerData: …"
        # into the name. The trailing `devices` listing emits clean
        # `Device <MAC> <name>` lines without any tag prefix.
        #
        # bluetoothctl also emits ANSI color codes even with no TTY;
        # strip them so trailing `\x1b[0m` doesn't end up in display
        # strings.
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        # Hyphen- or colon-separated MAC string used by bluez as a
        # fallback "name" when the device hasn't broadcast a real one —
        # not useful in a UI, so we skip these.
        mac_as_name_re = re.compile(r"^[0-9A-Fa-f]{2}(?:[-:][0-9A-Fa-f]{2}){5}$")

        devices: list[BluetoothDevice] = []
        seen: set[str] = set()
        for raw in stdout.split("\n"):
            line = ansi_re.sub("", raw).strip()
            # Filter discovery-event chatter — only the post-scan
            # `devices` listing has the authoritative name.
            if any(tag in line for tag in ("[NEW]", "[CHG]", "[DEL]")):
                continue
            match = re.match(r"(?:\[bluetoothctl\]>\s*)?Device\s+([0-9A-Fa-f:]{17})\s*(.*)", line)
            if not match:
                continue
            mac = match.group(1)
            if mac in seen:
                continue
            name = match.group(2).strip()
            # Drop devices that didn't broadcast a real name. Showing a
            # list of MAC strings to a user is just noise — the named
            # devices are what they actually want to pair with.
            if not name or mac_as_name_re.match(name):
                continue
            seen.add(mac)
            device_type = self._classify_device(mac)
            devices.append(BluetoothDevice(
                name=name,
                mac_address=mac,
                device_type=device_type,
            ))
        return devices

    def _prune_stale_cache(self) -> None:
        """Remove non-paired devices from BlueZ's cache.

        Without this, ``bluetoothctl devices`` returns everything ever
        seen — including devices long out of range — making it hard to
        tell what's actually nearby. Paired devices are kept so users
        can still see / reconnect to their saved pairings.
        """
        try:
            result = subprocess.run(
                ["bluetoothctl", "devices"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.split("\n"):
                m = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+", line)
                if not m:
                    continue
                mac = m.group(1)
                info = subprocess.run(
                    ["bluetoothctl", "info", mac],
                    capture_output=True, text=True, timeout=5
                )
                if "Paired: yes" in info.stdout:
                    continue
                subprocess.run(
                    ["bluetoothctl", "remove", mac],
                    capture_output=True, timeout=5
                )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass  # best-effort cleanup; scan still proceeds

    def _classify_device(self, mac_address: str) -> str:
        """Classify a device by querying its BlueZ icon/class."""
        try:
            result = self._run_bluetoothctl("info", mac_address, timeout=5.0)
            if result.returncode != 0:
                return "unknown"
            output = result.stdout.lower()
            if "icon: audio" in output or "audio sink" in output:
                return "audio_sink"
            if "icon: phone" in output:
                return "phone"
            if "audio source" in output:
                return "audio_source"
            return "unknown"
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return "unknown"

    def pair(self, mac_address: str) -> bool:
        try:
            # Set up pairing context up front. Idempotent — these stick
            # for subsequent commands in the same bluez session.
            self._run_bluetoothctl("agent", "NoInputNoOutput", timeout=5.0)
            self._run_bluetoothctl("default-agent", timeout=5.0)
            self._run_bluetoothctl("pairable", "on", timeout=5.0)

            # Check device state: paired? known to bluez at all?
            info = self._run_bluetoothctl("info", mac_address, timeout=5.0)
            already_paired = "Paired: yes" in info.stdout
            # `bluetoothctl info <mac>` returns "Device <mac> not available"
            # when bluez has no record (e.g., right after `remove`).
            device_known = "not available" not in info.stdout.lower()

            if already_paired:
                # Don't re-pair an already-bonded peer — most peripherals
                # (Arctis included) reject a fresh pair handshake from an
                # already-bonded peer with AuthenticationFailed. Treat as
                # success so the caller chains into connect() normally.
                logger.info("Bluetooth device already paired", mac=mac_address)
                return True

            if not device_known:
                # Device isn't in bluez's cache — typically because the
                # caller just called remove() to force a fresh pair, or
                # the device was never scanned. Do a brief scan to
                # repopulate the cache so `pair <MAC>` has a target.
                logger.info(
                    "Bluetooth device unknown to bluez, rescanning briefly",
                    mac=mac_address,
                )
                if not self._scan_briefly(mac_address, timeout=12.0):
                    logger.warning(
                        "Bluetooth pair: device not discoverable; ensure "
                        "it's powered on and in pairing mode",
                        mac=mac_address,
                    )
                    return False

            result = self._run_bluetoothctl("pair", mac_address, timeout=30.0)
            success = result.returncode == 0 or "already" in result.stdout.lower()
            if success:
                logger.info("Bluetooth device paired", mac=mac_address)
            else:
                logger.warning("Bluetooth pair failed", mac=mac_address, output=result.stdout)
            return success
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth pair error", mac=mac_address, error=str(e))
            return False

    def _scan_briefly(self, mac_address: str, timeout: float = 12.0) -> bool:
        """Run a short scan to (re)populate bluez's cache for one MAC.

        Used when the device was just removed (or never seen) and the
        next pair command needs a target. Returns True as soon as the
        device is known to bluez, False if it doesn't appear within
        the timeout.
        """
        import time
        proc = subprocess.Popen(
            ["bluetoothctl"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            proc.stdin.write("scan bredr\n")
            proc.stdin.flush()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                check = subprocess.run(
                    ["bluetoothctl", "info", mac_address],
                    capture_output=True, text=True, timeout=3
                )
                if "not available" not in check.stdout.lower():
                    return True
                time.sleep(1)
            return False
        finally:
            # Best-effort cleanup of the scan session.
            try:
                proc.stdin.write("scan off\nquit\n")
                proc.stdin.flush()
                proc.communicate(timeout=3)
            except (subprocess.TimeoutExpired, BrokenPipeError, ValueError, OSError):
                try:
                    proc.kill()
                except Exception:
                    pass

    def connect(self, mac_address: str) -> bool:
        try:
            # Read state once and reuse — saves a bluetoothctl round-trip
            # later when checking whether to verify the audio profile.
            info = self._run_bluetoothctl("info", mac_address, timeout=5.0)
            is_audio = self._is_audio_device(info.stdout)

            # bluez 5.x does NOT auto-connect profiles after a pair
            # handshake. "Connected: yes" right after pair only means
            # the ACL link is up — the A2DP/HFP profile won't engage
            # until something explicitly calls Device.Connect() to
            # trigger profile setup. Skipping the connect call and
            # waiting for an audio sink nothing has asked bluez to
            # create is the silent-fail trap that broke pairing on Pi
            # Zero 2W: btmon shows pair handshake → SDP → tear-down
            # without any L2CAP PSM 0x0019 (AVDTP) connection.
            # bluetoothctl connect is idempotent (safe when already
            # fully connected), so we always issue it and let the
            # regular path below handle the InProgress / A2DP-verify
            # flow.

            # 30s timeout (vs the previous 15s): first-attempt cold
            # connects do service discovery + auth + L2CAP setup over
            # BR/EDR, which can take 20s+ on a busy 2.4GHz band. A
            # premature timeout here cancels bluez's in-flight link
            # and bricks the next 5-10s of retry attempts with
            # `br-connection-busy`.
            result = self._run_bluetoothctl("connect", mac_address, timeout=30.0)
            stdout_lc = result.stdout.lower()
            connected = result.returncode == 0 or "successful" in stdout_lc

            # If bluez says "InProgress", a previous connect is still
            # negotiating. Poll the device's `Connected` flag for a
            # few seconds — that earlier attempt may finish on its own.
            if not connected and ("inprogress" in stdout_lc or "connection-busy" in stdout_lc):
                import time
                for _ in range(10):
                    time.sleep(1.0)
                    poll = self._run_bluetoothctl("info", mac_address, timeout=3.0)
                    if "Connected: yes" in poll.stdout:
                        connected = True
                        break

            if not connected:
                logger.warning("Bluetooth connect failed", mac=mac_address, output=result.stdout)
                return False

            # ACL link is up. For audio devices, verify the A2DP/HFP
            # profile actually negotiated — otherwise bluez quietly
            # retries AVDTP discovery every ~10s and produces audible
            # "click" sounds on the headphones. The most common failure
            # mode is the peer being bonded to another A2DP master
            # (e.g., user's phone) — Arctis-class buds only support one.
            #
            # If no bluez sink/source appears (audio profile failed),
            # disconnect explicitly so bluez stops the retry storm.
            # Caller treats this as a connect failure.
            if is_audio and not self._wait_for_audio_sink(mac_address, timeout=15.0):
                logger.warning(
                    "Bluetooth ACL connected but audio profile failed to negotiate "
                    "— peer is likely bonded to another device. Disconnecting to "
                    "prevent AVDTP retry storm.",
                    mac=mac_address,
                )
                # Best-effort cleanup; don't let a failed disconnect mask the real error.
                try:
                    self._run_bluetoothctl("disconnect", mac_address, timeout=5.0)
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
                return False

            logger.info("Bluetooth device connected", mac=mac_address)
            return True
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth connect error", mac=mac_address, error=str(e))
            return False

    def _is_audio_device(self, info_output: str) -> bool:
        """Detect whether a ``bluetoothctl info`` output describes an audio device.

        Audio devices need post-connect A2DP/HFP profile verification;
        non-audio devices (HID, etc.) do not.
        """
        out = info_output.lower()
        return (
            "icon: audio" in out
            or "audio sink" in out
            or "audio source" in out
            or "handsfree" in out
        )

    def _wait_for_audio_sink(self, mac_address: str, timeout: float = 15.0) -> bool:
        """Poll PulseAudio for a stable bluez audio sink/source.

        Why sink and not card: bluez registers a card the moment the
        ACL link comes up — that happens before AVDTP profile
        negotiation. The sink/source only appears AFTER A2DP/HFP
        successfully negotiates. Checking the card returns True too
        early; checking the sink reflects the actual audio path.

        Why "stable" (require 2s of continuous presence): bluez has
        been observed to transiently register a sink during AVDTP
        discovery, then tear it down when discovery times out. Holding
        the assertion for 2s rules out that flap.

        Covers both PipeWire-pulse names (``bluez_output.<MAC>``,
        ``bluez_input.<MAC>``) and legacy PulseAudio names
        (``bluez_sink.<MAC>``, ``bluez_source.<MAC>``), and both
        roles (Pi-as-source for headphones, Pi-as-sink for phone
        streaming in).
        """
        import time
        mac_underscored = mac_address.replace(":", "_").upper()
        markers = (
            f"bluez_sink.{mac_underscored}",
            f"bluez_output.{mac_underscored}",
            f"bluez_source.{mac_underscored}",
            f"bluez_input.{mac_underscored}",
        )

        deadline = time.monotonic() + timeout
        first_seen = None
        while time.monotonic() < deadline:
            try:
                sinks = subprocess.run(
                    ["pactl", "list", "sinks", "short"],
                    capture_output=True, text=True, timeout=3
                )
                sources = subprocess.run(
                    ["pactl", "list", "sources", "short"],
                    capture_output=True, text=True, timeout=3
                )
                combined = sinks.stdout + sources.stdout
                present = any(m in combined for m in markers)
                if present:
                    if first_seen is None:
                        first_seen = time.monotonic()
                    elif time.monotonic() - first_seen >= 2.0:
                        return True
                else:
                    # Marker disappeared — restart the stability timer.
                    first_seen = None
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            time.sleep(0.5)
        return False

    def disconnect(self, mac_address: str) -> bool:
        try:
            result = self._run_bluetoothctl("disconnect", mac_address, timeout=10.0)
            success = result.returncode == 0 or "successful" in result.stdout.lower()
            if success:
                logger.info("Bluetooth device disconnected", mac=mac_address)
            return success
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth disconnect error", mac=mac_address, error=str(e))
            return False

    def remove(self, mac_address: str) -> bool:
        try:
            result = self._run_bluetoothctl("remove", mac_address, timeout=10.0)
            success = result.returncode == 0
            if success:
                logger.info("Bluetooth device removed", mac=mac_address)
            return success
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth remove error", mac=mac_address, error=str(e))
            return False

    def set_discoverable(self, enabled: bool, timeout: int = 120) -> bool:
        try:
            if enabled:
                # Order matters: power on → pairable on → discoverable-timeout
                # → discoverable on. Without `pairable on`, iOS filters this
                # device out of its picker entirely even though bluez reports
                # Discoverable: yes. The timeout has to be set BEFORE enabling
                # discoverable so the first activation honors our value rather
                # than bluez's default (180s).
                self._run_bluetoothctl("power", "on", timeout=5.0)
                self._run_bluetoothctl("pairable", "on", timeout=5.0)
                self._run_bluetoothctl("discoverable-timeout", str(timeout), timeout=5.0)

            value = "on" if enabled else "off"
            result = self._run_bluetoothctl("discoverable", value, timeout=5.0)
            logger.info("Bluetooth discoverable", enabled=enabled, timeout=timeout)

            if enabled and result.returncode == 0:
                # Surface adapter state to the log so we can confirm
                # Powered/Pairable/Discoverable actually engaged without
                # needing an SSH round-trip.
                show = self._run_bluetoothctl("show", timeout=5.0)
                state = "; ".join(
                    line.strip() for line in show.stdout.splitlines()
                    if any(k in line for k in ("Powered:", "Discoverable:", "Pairable:", "Alias:"))
                )
                logger.info("Bluetooth adapter state", state=state)

            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth discoverable error", error=str(e))
            return False

    def get_paired_devices(self) -> list[BluetoothDevice]:
        try:
            result = self._run_bluetoothctl("paired-devices")
            if result.returncode != 0:
                return []

            devices: list[BluetoothDevice] = []
            for line in result.stdout.strip().split("\n"):
                match = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
                if match:
                    mac = match.group(1)
                    name = match.group(2).strip()
                    connected = self._is_connected(mac)
                    devices.append(BluetoothDevice(
                        name=name,
                        mac_address=mac,
                        device_type=self._classify_device(mac),
                        paired=True,
                        connected=connected,
                    ))
            return devices
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Failed to get paired devices", error=str(e))
            return []

    def get_connected_devices(self) -> list[BluetoothDevice]:
        paired = self.get_paired_devices()
        return [d for d in paired if d.connected]

    def trust(self, mac_address: str) -> bool:
        try:
            result = self._run_bluetoothctl("trust", mac_address, timeout=5.0)
            success = result.returncode == 0
            if success:
                logger.info("Bluetooth device trusted", mac=mac_address)
            return success
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth trust error", mac=mac_address, error=str(e))
            return False

    def _is_connected(self, mac_address: str) -> bool:
        """Check if a device is currently connected."""
        try:
            result = self._run_bluetoothctl("info", mac_address, timeout=5.0)
            return "Connected: yes" in result.stdout
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return False


class MacOSBluetoothProvider(BluetoothProvider):
    """macOS stub — Bluetooth pairing is not supported on dev machines."""

    def is_available(self) -> bool:
        return False

    def scan(self, timeout: float = 10.0) -> list[BluetoothDevice]:
        return []

    def pair(self, mac_address: str) -> bool:
        return False

    def connect(self, mac_address: str) -> bool:
        return False

    def disconnect(self, mac_address: str) -> bool:
        return False

    def remove(self, mac_address: str) -> bool:
        return False

    def set_discoverable(self, enabled: bool, timeout: int = 120) -> bool:
        return False

    def get_paired_devices(self) -> list[BluetoothDevice]:
        return []

    def get_connected_devices(self) -> list[BluetoothDevice]:
        return []

    def trust(self, mac_address: str) -> bool:
        return False


# Platform Factory
class PlatformFactory:
    """Factory for creating platform-specific providers"""
    
    @staticmethod
    def get_platform() -> str:
        """Get the current platform from environment or detect automatically"""
        platform_env = os.getenv("JARVIS_NODE_OS")
        if platform_env:
            return platform_env.upper()
        
        # Auto-detect based on system
        system = platform.system().lower()
        if system == "darwin":
            return "MACOS"
        elif system == "linux":
            return "PI"  # Assume Raspberry Pi for Linux
        else:
            return "PI"  # Default to PI
    
    @staticmethod
    def create_audio_provider() -> AudioProvider:
        """Create the appropriate audio provider for the current platform"""
        platform = PlatformFactory.get_platform()
        
        if platform == "MACOS":
            return MacOSAudioProvider()
        else:
            return PiAudioProvider()
    
    @staticmethod
    def create_network_discovery_provider() -> NetworkDiscoveryProvider:
        """Create the appropriate network discovery provider for the current platform"""
        platform = PlatformFactory.get_platform()
        
        if platform == "MACOS":
            return MacOSNetworkDiscoveryProvider()
        else:
            return PiNetworkDiscoveryProvider()
    
    @staticmethod
    def create_system_provider() -> SystemProvider:
        """Create the appropriate system provider for the current platform"""
        platform = PlatformFactory.get_platform()

        if platform == "MACOS":
            return MacOSSystemProvider()
        else:
            return PiSystemProvider()

    @staticmethod
    def create_bluetooth_provider() -> BluetoothProvider:
        """Create the appropriate Bluetooth provider for the current platform"""
        platform = PlatformFactory.get_platform()

        if platform == "MACOS":
            return MacOSBluetoothProvider()
        else:
            return PiBluetoothProvider()


# Convenience functions for easy access
def get_audio_provider() -> AudioProvider:
    """Get the audio provider for the current platform"""
    return PlatformFactory.create_audio_provider()


def get_network_discovery_provider() -> NetworkDiscoveryProvider:
    """Get the network discovery provider for the current platform"""
    return PlatformFactory.create_network_discovery_provider()


def get_system_provider() -> SystemProvider:
    """Get the system provider for the current platform"""
    return PlatformFactory.create_system_provider()


def get_bluetooth_provider() -> BluetoothProvider:
    """Get the Bluetooth provider for the current platform"""
    return PlatformFactory.create_bluetooth_provider()