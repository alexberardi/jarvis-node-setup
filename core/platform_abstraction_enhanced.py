"""
Enhanced platform abstraction layer for Jarvis Node.

This module provides platform-agnostic interfaces and platform-specific implementations
using dependency injection and composition patterns. It includes enhanced async methods
for network discovery.
"""

import os
import platform
import asyncio
import ipaddress
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, List, Any, Tuple

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

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
    
    @abstractmethod
    async def scan_network_async(self, network_range: str) -> List[str]:
        """Async version of network scanning"""
        pass
    
    @abstractmethod
    async def get_arp_table(self) -> List[Tuple[str, str]]:
        """Get ARP table entries as (ip, mac) tuples"""
        pass
    
    @abstractmethod
    async def scan_device_ports(self, ip: str, ports: List[int]) -> List[int]:
        """Scan common ports on a device"""
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
    """macOS-specific network discovery provider with enhanced async methods"""
    
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
    
    async def scan_network_async(self, network_range: str) -> List[str]:
        """Async network scanning for macOS with optimizations and fallbacks"""
        try:
            # First, try arp-scan which is faster but requires installation
            proc = await asyncio.create_subprocess_shell(
                "which arp-scan",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            _, _ = await proc.communicate()
            
            if proc.returncode == 0:
                # arp-scan is available
                proc = await asyncio.create_subprocess_shell(
                    f"arp-scan --localnet",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                stdout, _ = await proc.communicate()
                
                active_hosts = []
                subnet = ipaddress.IPv4Network(network_range)
                
                for line in stdout.decode().splitlines():
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            ip = parts[0]
                            if ipaddress.IPv4Address(ip) in subnet:
                                active_hosts.append(ip)
                        except ValueError:
                            pass  # Invalid IP address format, skip
                            
                return active_hosts
            else:
                # Fallback to ping sweep with rate limiting
                return await self._ping_sweep_async(network_range)
                
        except Exception as e:
            logger.error(f"Error in async network scan: {e}")
            return []
    
    async def _ping_sweep_async(self, network_range: str) -> List[str]:
        """Fallback ping sweep with controlled concurrency for macOS"""
        active_hosts = []
        subnet = ipaddress.IPv4Network(network_range)
        semaphore = asyncio.Semaphore(25)  # Limit concurrent pings
        
        async def ping_host(ip):
            async with semaphore:
                try:
                    proc = await asyncio.create_subprocess_shell(
                        f"ping -c 1 -W 1 -t 1 {ip}",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=2.0)
                    if proc.returncode == 0:
                        return ip
                    return None
                except asyncio.TimeoutError:
                    return None
                except OSError:
                    return None
                
        tasks = []
        for ip in subnet.hosts():
            ip_str = str(ip)
            tasks.append(ping_host(ip_str))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, str):
                active_hosts.append(result)
                
        return active_hosts
        
    async def get_arp_table(self) -> List[Tuple[str, str]]:
        """Get ARP table entries for macOS"""
        entries = []
        try:
            proc = await asyncio.create_subprocess_shell(
                "arp -a",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            
            for line in stdout.decode().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    ip = parts[1].strip("()")
                    mac = parts[3]
                    if mac != "ff:ff:ff:ff:ff:ff" and not mac.startswith("incomplete"):
                        entries.append((ip, mac))
        except Exception as e:
            logger.error(f"Error getting ARP table: {e}")
            
        return entries
        
    async def scan_device_ports(self, ip: str, ports: List[int]) -> List[int]:
        """Scan common ports on a macOS device"""
        open_ports = []
        semaphore = asyncio.Semaphore(10)  # Limit concurrent connections
        
        async def check_port(port):
            async with semaphore:
                try:
                    # Create socket with timeout
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port),
                        timeout=0.5
                    )
                    writer.close()
                    await writer.wait_closed()
                    return port
                except (OSError, asyncio.TimeoutError):
                    return None  # Port closed or connection failed
        
        tasks = [check_port(port) for port in ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, int):
                open_ports.append(result)
                
        return open_ports


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
    """Raspberry Pi/Linux-specific network discovery provider with enhanced async methods"""
    
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
    
    async def scan_network_async(self, network_range: str) -> List[str]:
        """Async network scanning for Raspberry Pi/Linux with optimizations"""
        try:
            # Try to use nmap for efficient scanning
            proc = await asyncio.create_subprocess_shell(
                f"nmap -sn -n --max-retries=1 --max-scan-delay=10ms {network_range}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
            
            active_hosts = []
            for line in stdout.decode().splitlines():
                if "Nmap scan report for" in line:
                    parts = line.split()
                    ip = parts[-1].strip("()")
                    active_hosts.append(ip)
                    
            if active_hosts:
                return active_hosts
            else:
                # Fallback to ping sweep
                return await self._ping_sweep_async(network_range)
                
        except Exception as e:
            logger.error(f"Error in async network scan: {e}")
            # Fallback to ping sweep
            return await self._ping_sweep_async(network_range)
    
    async def _ping_sweep_async(self, network_range: str) -> List[str]:
        """Fallback ping sweep for Raspberry Pi/Linux with controlled concurrency"""
        active_hosts = []
        subnet = ipaddress.IPv4Network(network_range)
        semaphore = asyncio.Semaphore(25)  # Limit concurrent pings
        
        async def ping_host(ip):
            async with semaphore:
                try:
                    proc = await asyncio.create_subprocess_shell(
                        f"ping -c 1 -W 1 {ip}",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL
                    )
                    await asyncio.wait_for(proc.communicate(), timeout=2.0)
                    if proc.returncode == 0:
                        return ip
                    return None
                except asyncio.TimeoutError:
                    return None
                except OSError:
                    return None
                
        tasks = []
        for ip in subnet.hosts():
            ip_str = str(ip)
            tasks.append(ping_host(ip_str))
            
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, str):
                active_hosts.append(result)
                
        return active_hosts
        
    async def get_arp_table(self) -> List[Tuple[str, str]]:
        """Get ARP table entries for Raspberry Pi/Linux"""
        entries = []
        
        # First try with ip neigh
        try:
            proc = await asyncio.create_subprocess_shell(
                "ip neigh show",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            
            for line in stdout.decode().splitlines():
                parts = line.split()
                if len(parts) >= 4 and "lladdr" in line:
                    ip = parts[0]
                    mac_index = parts.index("lladdr") + 1
                    if mac_index < len(parts):
                        mac = parts[mac_index]
                        entries.append((ip, mac))
                        
            if entries:
                return entries
        except Exception as e:
            logger.error(f"Error getting ARP table with ip neigh: {e}")
        
        # Fallback to arp command
        try:
            proc = await asyncio.create_subprocess_shell(
                "arp -a",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, _ = await proc.communicate()
            
            for line in stdout.decode().splitlines():
                parts = line.split()
                if len(parts) >= 4:
                    ip = parts[1].strip("()")
                    mac = parts[3]
                    if mac != "ff:ff:ff:ff:ff:ff" and not mac.startswith("incomplete"):
                        entries.append((ip, mac))
        except Exception as e:
            logger.error(f"Error getting ARP table with arp -a: {e}")
            
        return entries
        
    async def scan_device_ports(self, ip: str, ports: List[int]) -> List[int]:
        """Scan common ports on a Raspberry Pi/Linux device"""
        open_ports = []
        semaphore = asyncio.Semaphore(10)  # Limit concurrent connections
        
        async def check_port(port):
            async with semaphore:
                try:
                    # Create socket with timeout
                    reader, writer = await asyncio.wait_for(
                        asyncio.open_connection(ip, port),
                        timeout=0.5
                    )
                    writer.close()
                    await writer.wait_closed()
                    return port
                except (OSError, asyncio.TimeoutError):
                    return None  # Port closed or connection failed
        
        tasks = [check_port(port) for port in ports]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        
        for result in results:
            if isinstance(result, int):
                open_ports.append(result)
                
        return open_ports


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