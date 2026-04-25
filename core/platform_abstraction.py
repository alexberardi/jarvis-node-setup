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

    def cancel_playback(self) -> bool:
        """Cancel any active audio playback (barge-in support).

        Sets the cancel event (checked by playback loops and used to
        pre-empt future playback calls) and kills the active subprocess.
        Thread-safe — can be called from any thread.

        Returns True if a process was cancelled.
        """
        self._cancel_event.set()
        with self._playback_lock:
            proc = self._playback_proc
            if proc is None:
                return False
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
        # PyAudio on Pi Zero 2 W + softvol + asym + plughw was glitching
        # audibly even when network chunks arrived on time. aplay is the
        # same binary the short-TTS path uses successfully, and it talks
        # directly to the ALSA device we've configured in /etc/asound.conf.
        format_arg = {1: "U8", 2: "S16_LE", 3: "S24_LE", 4: "S32_LE"}.get(
            sample_width, "S16_LE"
        )
        cmd = [
            "aplay",
            "-D", "output",      # the softvol alias defined in asound.conf
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

    def scan(self, timeout: float = 10.0) -> list[BluetoothDevice]:
        try:
            # Start scan, wait, then collect devices
            subprocess.run(
                ["bluetoothctl", "scan", "on"],
                capture_output=True, text=True, timeout=3.0
            )
            import time
            time.sleep(min(timeout, 15.0))
            subprocess.run(
                ["bluetoothctl", "scan", "off"],
                capture_output=True, text=True, timeout=3.0
            )

            result = self._run_bluetoothctl("devices")
            if result.returncode != 0:
                return []

            devices: list[BluetoothDevice] = []
            for line in result.stdout.strip().split("\n"):
                match = re.match(r"Device\s+([0-9A-Fa-f:]{17})\s+(.*)", line)
                if match:
                    mac = match.group(1)
                    name = match.group(2).strip()
                    device_type = self._classify_device(mac)
                    devices.append(BluetoothDevice(
                        name=name,
                        mac_address=mac,
                        device_type=device_type,
                    ))
            return devices
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth scan failed", error=str(e))
            return []

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
            # Set agent for headless pairing
            self._run_bluetoothctl("agent", "NoInputNoOutput", timeout=5.0)
            self._run_bluetoothctl("default-agent", timeout=5.0)

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

    def connect(self, mac_address: str) -> bool:
        try:
            result = self._run_bluetoothctl("connect", mac_address, timeout=15.0)
            success = result.returncode == 0 or "successful" in result.stdout.lower()
            if success:
                logger.info("Bluetooth device connected", mac=mac_address)
            else:
                logger.warning("Bluetooth connect failed", mac=mac_address, output=result.stdout)
            return success
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Bluetooth connect error", mac=mac_address, error=str(e))
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
            value = "on" if enabled else "off"
            result = self._run_bluetoothctl("discoverable", value, timeout=5.0)
            if enabled and result.returncode == 0:
                self._run_bluetoothctl("discoverable-timeout", str(timeout), timeout=5.0)
            logger.info("Bluetooth discoverable", enabled=enabled, timeout=timeout)
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