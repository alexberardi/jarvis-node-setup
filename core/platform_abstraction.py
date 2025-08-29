"""
Platform abstraction layer for Jarvis Node.

This module provides platform-agnostic interfaces and platform-specific implementations
using dependency injection and composition patterns.
"""

import os
import platform
from abc import ABC, abstractmethod
from typing import Dict, List, Optional, Any
import subprocess


class AudioProvider(ABC):
    """Abstract interface for audio operations"""
    
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
    
    def play_audio_file(self, file_path: str, volume: float = 1.0) -> bool:
        try:
            # macOS doesn't have a direct volume control with afplay
            # We could use sox for volume control if needed
            result = subprocess.run(
                ["afplay", file_path],
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0
        except Exception as e:
            print(f"Error playing audio file: {e}")
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
            print(f"Error getting audio devices: {e}")
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
            print(f"Error in dns-sd discovery: {e}")
        
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
                print(f"Error querying {service}: {e}")
        
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
            print(f"Error scanning network: {e}")
        
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
            print(f"Error installing package {package_name}: {e}")
            return False
    
    def get_audio_config_path(self) -> str:
        return os.path.expanduser("~/.config/jarvis-node/audio_config.json")


# Raspberry Pi/Linux Implementations
class PiAudioProvider(AudioProvider):
    """Raspberry Pi/Linux-specific audio provider using ALSA"""
    
    def play_audio_file(self, file_path: str, volume: float = 1.0) -> bool:
        try:
            # Use sox for volume control and aplay for playback
            if volume != 1.0:
                result = subprocess.run(
                    f"sox {file_path} -t wav - vol {volume} | aplay -D output",
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=30
                )
            else:
                result = subprocess.run(
                    ["aplay", "-D", "output", file_path],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
            return result.returncode == 0
        except Exception as e:
            print(f"Error playing audio file: {e}")
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
            print(f"Error playing chime: {e}")
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
            print(f"Error getting audio devices: {e}")
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
            print(f"Error in avahi-browse discovery: {e}")
        
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
            print(f"Error in systemd-resolve discovery: {e}")
        
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
            print(f"Error scanning network: {e}")
        
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
            print(f"Error installing package {package_name}: {e}")
            return False
    
    def get_audio_config_path(self) -> str:
        return "/etc/jarvis-node/audio_config.json"


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