import json
import select
import socket
import struct
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import List, Dict, Any, Optional, Set

import ipaddress
import requests
import urllib3

from utils.config_service import Config

# Suppress SSL warnings for local network scanning
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


@dataclass
class DiscoveredDevice:
    """Represents a device discovered on the network"""
    ip_address: str
    hostname: Optional[str] = None
    device_type: Optional[str] = None
    manufacturer: Optional[str] = None
    model: Optional[str] = None
    open_ports: List[int] = None  # type: ignore
    services: List[str] = None  # type: ignore
    mac_address: Optional[str] = None
    is_jarvis_node: bool = False
    confidence_score: float = 0.0
    
    def __post_init__(self):
        if self.open_ports is None:
            self.open_ports = []
        if self.services is None:
            self.services = []
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            "ip_address": self.ip_address,
            "hostname": self.hostname,
            "device_type": self.device_type,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "open_ports": self.open_ports,
            "services": self.services,
            "mac_address": self.mac_address,
            "is_jarvis_node": self.is_jarvis_node,
            "confidence_score": self.confidence_score
        }


class NetworkDiscoveryService:
    """Service for discovering devices and other Jarvis nodes on the network"""
    
    def __init__(self):
        self.config = Config()
        self.discovered_devices: Dict[str, DiscoveredDevice] = {}
        self.scan_in_progress = False
        self.last_scan_time = 0
        self.scan_lock = threading.Lock()
        
        # mDNS discovery
        self.mdns_discovered: Dict[str, Dict[str, Any]] = {}
        
        # UPnP discovery
        self.upnp_discovered: Dict[str, Dict[str, Any]] = {}
        
        # DHCP fingerprinting
        self.dhcp_discovered: Dict[str, Dict[str, Any]] = {}
        
        # Common ports for smart devices and services
        self.common_ports = {
            22: "SSH",
            80: "HTTP",
            443: "HTTPS",
            1883: "MQTT",
            8883: "MQTT_SSL",
            8080: "HTTP_ALT",
            8081: "HTTP_ALT",
            8082: "HTTP_ALT",
            8083: "HTTP_ALT",
            8084: "HTTP_ALT",
            8085: "HTTP_ALT",
            8086: "HTTP_ALT",
            8087: "HTTP_ALT",
            8088: "HTTP_ALT",
            8089: "HTTP_ALT",
            8090: "HTTP_ALT",
            8123: "Home Assistant",
            9000: "HTTP_ALT",
            9001: "HTTP_ALT",
            9002: "HTTP_ALT",
            9003: "HTTP_ALT",
            9004: "HTTP_ALT",
            9005: "HTTP_ALT",
            9006: "HTTP_ALT",
            9007: "HTTP_ALT",
            9008: "HTTP_ALT",
            9009: "HTTP_ALT",
            9010: "HTTP_ALT",
            1900: "UPnP",
            5353: "mDNS",
            5432: "PostgreSQL",
            3306: "MySQL",
            6379: "Redis",
            27017: "MongoDB"
        }
        
        # Jarvis-specific identifiers
        self.jarvis_identifiers = {
            "hostname_patterns": ["jarvis", "zero", "node"],
            "services": ["jarvis-node", "jarvis-command-center"],
            "ports": [1883, 8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090]
        }
    
    def get_network_range(self) -> List[str]:
        """Get the local network range to scan"""
        try:
            # Get local IP by connecting to a remote address
            # This ensures we get the actual network interface IP
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                local_ip = s.getsockname()[0]
            
            print(f"🔍 Detected local IP: {local_ip}")
            
            # Parse IP to get network
            ip_parts = local_ip.split('.')
            network_base = f"{ip_parts[0]}.{ip_parts[1]}.{ip_parts[2]}"
            
            print(f"🔍 Scanning network: {network_base}.0/24")
            
            # Generate IP list for scanning
            ips_to_scan = []
            for i in range(1, 255):
                ips_to_scan.append(f"{network_base}.{i}")
            
            return ips_to_scan
            
        except Exception as e:
            print(f"Error determining network range: {e}")
            # Fallback to common home network ranges
            fallback_ranges = [
                "192.168.1", "192.168.0", "10.0.0", "10.0.1", 
                "172.16.0", "172.16.1", "172.17.0", "172.18.0"
            ]
            
            for base in fallback_ranges:
                print(f"🔍 Trying fallback network: {base}.0/24")
                ips_to_scan = []
                for i in range(1, 255):
                    ips_to_scan.append(f"{base}.{i}")
                return ips_to_scan
            
            # Final fallback
            return [f"192.168.1.{i}" for i in range(1, 255)]
    
    def scan_port(self, ip: str, port: int, timeout: float = 1.0) -> bool:
        """Check if a specific port is open on an IP"""
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            result = sock.connect_ex((ip, port))
            sock.close()
            return result == 0
        except Exception:
            return False
    
    def get_hostname(self, ip: str) -> Optional[str]:
        """Try to get hostname for an IP using multiple methods"""
        hostname = None
        
        # Method 1: Standard reverse DNS lookup
        try:
            hostname = socket.gethostbyaddr(ip)[0]
            if hostname and hostname != ip:
                return hostname
        except Exception:
            pass
        
        # Method 2: Try using nslookup command
        try:
            result = subprocess.run(
                ["nslookup", ip], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                for line in lines:
                    if "name =" in line.lower():
                        parts = line.split('=')
                        if len(parts) > 1:
                            potential_hostname = parts[1].strip().rstrip('.')
                            if potential_hostname and potential_hostname != ip:
                                return potential_hostname
        except Exception:
            pass
        
        # Method 3: Try using dig command
        try:
            result = subprocess.run(
                ["dig", "+short", "-x", ip], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            if result.returncode == 0:
                hostname = result.stdout.strip().rstrip('.')
                if hostname and hostname != ip:
                    return hostname
        except Exception:
            pass
        
        return None
    
    def get_mac_address(self, ip: str) -> Optional[str]:
        """Try to get MAC address for an IP using arp"""
        try:
            result = subprocess.run(
                ["arp", "-n", ip], 
                capture_output=True, 
                text=True, 
                timeout=5
            )
            if result.returncode == 0:
                lines = result.stdout.strip().split('\n')
                if len(lines) > 1:
                    parts = lines[1].split()
                    if len(parts) >= 3:
                        return parts[2]
            return None
        except Exception:
            return None
    
    def http_fingerprint_device(self, ip: str, port: int = 80) -> Dict[str, Any]:
        """Attempt to identify device type via HTTP requests with comprehensive device detection"""
        fingerprint = {
            "device_type": None,
            "manufacturer": None,
            "model": None,
            "software": None,
            "confidence": 0.0
        }
        
        # Common ports to try for HTTP fingerprinting
        ports_to_try = [port]
        if port == 80:
            ports_to_try.extend([443, 8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090, 9000, 9001, 9002])
        
        for test_port in ports_to_try:
            try:
                # Try HTTP
                if test_port != 443:
                    url = f"http://{ip}:{test_port}"
                    response = requests.get(url, timeout=2, allow_redirects=True)
                else:
                    url = f"https://{ip}:{test_port}"
                    response = requests.get(url, timeout=2, allow_redirects=True, verify=False)
                
                if response.status_code == 200:
                    content = response.text.lower()
                    headers = {k.lower(): v.lower() for k, v in response.headers.items()}
                    final_url = response.url.lower()
                    
                    # Apple Devices
                    if any(keyword in content for keyword in ["homepod", "airplay", "apple tv", "apple-tv"]):
                        if "homepod" in content:
                            fingerprint.update({
                                "device_type": "smart_speaker",
                                "manufacturer": "Apple",
                                "model": "HomePod",
                                "confidence": 0.9
                            })
                        elif "apple tv" in content or "appletv" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "Apple",
                                "model": "Apple TV",
                                "confidence": 0.9
                            })
                        elif "airplay" in content:
                            fingerprint.update({
                                "device_type": "airplay_device",
                                "manufacturer": "Apple",
                                "confidence": 0.8
                            })
                    
                    # Amazon Devices
                    elif any(keyword in content for keyword in ["echo", "alexa", "amazon", "fire tv", "firetv"]):
                        if "echo" in content or "alexa" in content:
                            fingerprint.update({
                                "device_type": "smart_speaker",
                                "manufacturer": "Amazon",
                                "model": "Echo",
                                "confidence": 0.9
                            })
                        elif "fire tv" in content or "firetv" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "Amazon",
                                "model": "Fire TV",
                                "confidence": 0.9
                            })
                    
                    # Google Devices
                    elif any(keyword in content for keyword in ["google home", "nest", "chromecast", "googlecast"]):
                        if "google home" in content or "nest" in content:
                            fingerprint.update({
                                "device_type": "smart_speaker",
                                "manufacturer": "Google",
                                "model": "Google Home/Nest",
                                "confidence": 0.9
                            })
                        elif "chromecast" in content or "googlecast" in content:
                            fingerprint.update({
                                "device_type": "media_device",
                                "manufacturer": "Google",
                                "model": "Chromecast",
                                "confidence": 0.9
                            })
                    
                    # Sonos Devices
                    elif "sonos" in content or "/sonos/" in final_url:
                        fingerprint.update({
                            "device_type": "smart_speaker",
                            "manufacturer": "Sonos",
                            "model": "Sonos Speaker",
                            "confidence": 0.9
                        })
                    
                    # Philips Hue
                    elif any(keyword in content for keyword in ["philips hue", "hue bridge", "/api/"]):
                        fingerprint.update({
                            "device_type": "philips_hue_bridge",
                            "manufacturer": "Philips",
                            "model": "Hue Bridge",
                            "confidence": 0.9
                        })
                    
                    # Home Assistant
                    elif any(keyword in content for keyword in ["home assistant", "hass", "lovelace", "supervisor"]):
                        fingerprint.update({
                            "device_type": "home_assistant",
                            "software": "Home Assistant",
                            "confidence": 0.9
                        })
                    
                    # Plex Media Server
                    elif any(keyword in content for keyword in ["plex", "plex media server", "plex.tv"]):
                        fingerprint.update({
                            "device_type": "media_server",
                            "software": "Plex",
                            "confidence": 0.9
                        })
                    
                    # Jellyfin Media Server
                    elif "jellyfin" in content:
                        fingerprint.update({
                            "device_type": "media_server",
                            "software": "Jellyfin",
                            "confidence": 0.9
                        })
                    
                    # Emby Media Server
                    elif "emby" in content:
                        fingerprint.update({
                            "device_type": "media_server",
                            "software": "Emby",
                            "confidence": 0.9
                        })
                    
                    # Portainer (Docker Management)
                    elif "portainer" in content:
                        fingerprint.update({
                            "device_type": "management_interface",
                            "software": "Portainer",
                            "confidence": 0.9
                        })
                    
                    # Router/Network Devices
                    elif any(keyword in content for keyword in ["router", "gateway", "admin", "configuration", "network", "wifi", "wireless"]):
                        if "asus" in content:
                            fingerprint.update({
                                "device_type": "router",
                                "manufacturer": "ASUS",
                                "confidence": 0.8
                            })
                        elif "netgear" in content:
                            fingerprint.update({
                                "device_type": "router",
                                "manufacturer": "Netgear",
                                "confidence": 0.8
                            })
                        elif "linksys" in content:
                            fingerprint.update({
                                "device_type": "router",
                                "manufacturer": "Linksys",
                                "confidence": 0.8
                            })
                        elif "tp-link" in content or "tplink" in content:
                            fingerprint.update({
                                "device_type": "router",
                                "manufacturer": "TP-Link",
                                "confidence": 0.8
                            })
                        else:
                            fingerprint.update({
                                "device_type": "router",
                                "confidence": 0.7
                            })
                    
                    # Smart Plugs/Switches
                    elif any(keyword in content for keyword in ["smart plug", "smart switch", "wifi plug", "wifi switch", "tuya", "smartlife", "gosund", "kasa"]):
                        if "tuya" in content or "smartlife" in content:
                            fingerprint.update({
                                "device_type": "smart_plug",
                                "manufacturer": "Tuya",
                                "confidence": 0.8
                            })
                        elif "kasa" in content:
                            fingerprint.update({
                                "device_type": "smart_plug",
                                "manufacturer": "TP-Link",
                                "model": "Kasa",
                                "confidence": 0.8
                            })
                        elif "gosund" in content:
                            fingerprint.update({
                                "device_type": "smart_plug",
                                "manufacturer": "Gosund",
                                "confidence": 0.8
                            })
                        else:
                            fingerprint.update({
                                "device_type": "smart_plug",
                                "confidence": 0.7
                            })
                    
                    # Printers
                    elif any(keyword in content for keyword in ["printer", "print", "scanner", "fax", "hp", "canon", "epson", "brother"]):
                        if "hp" in content:
                            fingerprint.update({
                                "device_type": "printer",
                                "manufacturer": "HP",
                                "confidence": 0.8
                            })
                        elif "canon" in content:
                            fingerprint.update({
                                "device_type": "printer",
                                "manufacturer": "Canon",
                                "confidence": 0.8
                            })
                        elif "epson" in content:
                            fingerprint.update({
                                "device_type": "printer",
                                "manufacturer": "Epson",
                                "confidence": 0.8
                            })
                        elif "brother" in content:
                            fingerprint.update({
                                "device_type": "printer",
                                "manufacturer": "Brother",
                                "confidence": 0.8
                            })
                        else:
                            fingerprint.update({
                                "device_type": "printer",
                                "confidence": 0.7
                            })
                    
                    # Smart TVs
                    elif any(keyword in content for keyword in ["smart tv", "smarttv", "roku", "samsung", "lg", "vizio", "tcl", "hisense"]):
                        if "roku" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "Roku",
                                "confidence": 0.9
                            })
                        elif "samsung" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "Samsung",
                                "confidence": 0.8
                            })
                        elif "lg" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "LG",
                                "confidence": 0.8
                            })
                        elif "vizio" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "Vizio",
                                "confidence": 0.8
                            })
                        elif "tcl" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "TCL",
                                "confidence": 0.8
                            })
                        elif "hisense" in content:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "manufacturer": "Hisense",
                                "confidence": 0.8
                            })
                        else:
                            fingerprint.update({
                                "device_type": "smart_tv",
                                "confidence": 0.7
                            })
                    
                    # Security Cameras
                    elif any(keyword in content for keyword in ["camera", "surveillance", "security", "ring", "nest cam", "arlo", "wyze"]):
                        if "ring" in content:
                            fingerprint.update({
                                "device_type": "security_camera",
                                "manufacturer": "Ring",
                                "confidence": 0.8
                            })
                        elif "nest" in content:
                            fingerprint.update({
                                "device_type": "security_camera",
                                "manufacturer": "Google",
                                "model": "Nest Cam",
                                "confidence": 0.8
                            })
                        elif "arlo" in content:
                            fingerprint.update({
                                "device_type": "security_camera",
                                "manufacturer": "Arlo",
                                "confidence": 0.8
                            })
                        elif "wyze" in content:
                            fingerprint.update({
                                "device_type": "security_camera",
                                "manufacturer": "Wyze",
                                "confidence": 0.8
                            })
                        else:
                            fingerprint.update({
                                "device_type": "security_camera",
                                "confidence": 0.7
                            })
                    
                    # Smart Thermostats
                    elif any(keyword in content for keyword in ["thermostat", "nest thermostat", "ecobee", "honeywell"]):
                        if "nest" in content:
                            fingerprint.update({
                                "device_type": "smart_thermostat",
                                "manufacturer": "Google",
                                "model": "Nest Thermostat",
                                "confidence": 0.8
                            })
                        elif "ecobee" in content:
                            fingerprint.update({
                                "device_type": "smart_thermostat",
                                "manufacturer": "Ecobee",
                                "confidence": 0.8
                            })
                        elif "honeywell" in content:
                            fingerprint.update({
                                "device_type": "smart_thermostat",
                                "manufacturer": "Honeywell",
                                "confidence": 0.8
                            })
                        else:
                            fingerprint.update({
                                "device_type": "smart_thermostat",
                                "confidence": 0.7
                            })
                    
                    # Check server headers for additional clues
                    if "server" in headers:
                        server = headers["server"]
                        if "nginx" in server:
                            fingerprint["software"] = "nginx"
                        elif "apache" in server:
                            fingerprint["software"] = "apache"
                        elif "lighttpd" in server:
                            fingerprint["software"] = "lighttpd"
                        elif "iis" in server:
                            fingerprint["software"] = "IIS"
                        elif "jetty" in server:
                            fingerprint["software"] = "Jetty"
                        elif "tomcat" in server:
                            fingerprint["software"] = "Tomcat"
                    
                    # If we found a device, break out of port loop
                    if fingerprint["device_type"]:
                        break
                        
            except Exception:
                continue
        
        return fingerprint
    
    def _fingerprint_device(self, device: DiscoveredDevice) -> Optional[Dict[str, Any]]:
        """Helper method to fingerprint a device using HTTP requests"""
        try:
            # Try common HTTP ports that are open
            for port in [80, 443, 8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090, 9000, 9001, 9002]:
                if port in device.open_ports:
                    fingerprint = self.http_fingerprint_device(device.ip_address, port)
                    if fingerprint and fingerprint.get("device_type"):
                        return fingerprint
            
            # If no open ports or no identification, try common HTTP ports anyway
            # Many smart devices might not show up in port scans but respond to HTTP
            for port in [80, 443, 8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090, 9000, 9001, 9002]:
                fingerprint = self.http_fingerprint_device(device.ip_address, port)
                if fingerprint and fingerprint.get("device_type"):
                    return fingerprint
            
            return None
            
        except Exception as e:
            print(f"❌ Error in _fingerprint_device for {device.ip_address}: {e}")
            return None
    
    def discover_mdns_services(self) -> Dict[str, Dict[str, Any]]:
        """Discover devices using mDNS (Bonjour) - Home Assistant style"""
        discovered = {}
        
        try:
            print("🔍 Starting advanced mDNS discovery (Home Assistant style)...")
            
            # Use platform abstraction for mDNS discovery
            from core.platform_abstraction import get_network_discovery_provider
            network_provider = get_network_discovery_provider()
            
            mdns_services = network_provider.discover_mdns_services()
            
            # Process discovered services
            for service_type, service_data in mdns_services.items():
                if isinstance(service_data, list):
                    for service in service_data:
                        if isinstance(service, dict):
                            # Handle structured service data
                            ip = service.get("address", "")
                            hostname = service.get("hostname", "")
                            service_name = service.get("service_name", service_type)
                            
                            if ip and ip not in discovered:
                                discovered[ip] = {
                                    "hostname": hostname,
                                    "device_type": self._classify_mdns_service(service_type),
                                    "manufacturer": self._get_manufacturer_from_service(service_type),
                                    "model": service_name,
                                    "services": [service_type],
                                    "confidence_score": 0.8
                                }
                        elif isinstance(service, str):
                            # Handle simple string data
                            if service and service not in discovered:
                                discovered[service] = {
                                    "hostname": service,
                                    "device_type": self._classify_mdns_service(service_type),
                                    "manufacturer": self._get_manufacturer_from_service(service_type),
                                    "model": service,
                                    "services": [service_type],
                                    "confidence_score": 0.7
                                }
                elif isinstance(service_data, str):
                    # Handle string data directly
                    if service_data and service_data not in discovered:
                        discovered[service_data] = {
                            "hostname": service_data,
                            "device_type": self._classify_mdns_service(service_type),
                            "manufacturer": self._get_manufacturer_from_service(service_type),
                            "model": service_data,
                            "services": [service_type],
                            "confidence_score": 0.6
                        }
            
            print(f"✅ mDNS discovery found {len(discovered)} devices")
            return discovered
            
        except Exception as e:
            print(f"❌ Error in mDNS discovery: {e}")
            return {}
    
    def _classify_mdns_service(self, service_type: str) -> str:
        """Classify device type based on mDNS service"""
        service_lower = service_type.lower()
        
        # Apple devices
        if "airplay" in service_lower or "raop" in service_lower:
            return "airplay_device"
        elif "homekit" in service_lower:
            return "homekit_device"
        elif "apple-mobdev2" in service_lower:
            return "apple_device"
        
        # Google devices
        elif "googlecast" in service_lower or "googlezone" in service_lower:
            return "media_device"
        elif "nest" in service_lower:
            return "smart_thermostat"
        
        # Smart TVs
        elif "webostv" in service_lower or "lgwebostv" in service_lower:
            return "smart_tv"
        elif "roku" in service_lower:
            return "smart_tv"
        elif "samsungtv" in service_lower:
            return "smart_tv"
        
        # Smart speakers
        elif "sonos" in service_lower:
            return "smart_speaker"
        elif "spotify-connect" in service_lower:
            return "media_device"
        
        # IoT protocols
        elif "hap" in service_lower:
            return "homekit_device"
        elif "zigbee" in service_lower:
            return "zigbee_controller"
        elif "zwave" in service_lower:
            return "zwave_controller"
        elif "thread" in service_lower:
            return "thread_controller"
        elif "matter" in service_lower:
            return "matter_controller"
        
        # Media servers
        elif "plex" in service_lower:
            return "media_server"
        elif "jellyfin" in service_lower:
            return "media_server"
        elif "emby" in service_lower:
            return "media_server"
        elif "musicassistant" in service_lower:
            return "media_server"
        
        # Network services
        elif "http" in service_lower:
            return "web_device"
        elif "ssh" in service_lower:
            return "linux_device"
        elif "ftp" in service_lower or "sftp" in service_lower:
            return "file_server"
        
        # Printers
        elif "ipp" in service_lower or "printer" in service_lower or "pdl-datastream" in service_lower:
            return "printer"
        
        return "mdns_device"
    
    def _get_manufacturer_from_service(self, service_type: str) -> Optional[Dict[str, str]]:
        """Get manufacturer info from mDNS service type"""
        service_lower = service_type.lower()
        
        # Apple devices
        if "airplay" in service_lower or "raop" in service_lower or "homekit" in service_lower or "apple-mobdev2" in service_lower:
            return {"manufacturer": "Apple"}
        
        # Google devices
        elif "googlecast" in service_lower or "googlezone" in service_lower or "nest" in service_lower:
            return {"manufacturer": "Google"}
        
        # LG devices
        elif "webostv" in service_lower or "lgwebostv" in service_lower:
            return {"manufacturer": "LG"}
        
        # Samsung devices
        elif "samsungtv" in service_lower:
            return {"manufacturer": "Samsung"}
        
        # Roku devices
        elif "roku" in service_lower:
            return {"manufacturer": "Roku"}
        
        # Sonos devices
        elif "sonos" in service_lower:
            return {"manufacturer": "Sonos"}
        
        return None
    
    def discover_device_specific_apis(self) -> Dict[str, Dict[str, Any]]:
        """Discover devices using device-specific APIs (Home Assistant style)"""
        discovered = {}
        
        try:
            print("🔍 Starting device-specific API discovery...")
            
            # Get network range for scanning - limit to first 50 IPs to avoid hanging
            network_ips = self.get_network_range()[:50]  # Limit to first 50 IPs
            print(f"  🔍 Scanning {len(network_ips)} IPs for device APIs (limited to avoid hanging)...")
            
            # Use shorter timeouts and fewer workers to prevent hanging
            with ThreadPoolExecutor(max_workers=5) as executor:
                # LG TV WebOS API discovery
                print("    🔍 Scanning for LG TV WebOS APIs...")
                lg_futures = {executor.submit(self._discover_lg_tv, ip): ip for ip in network_ips}
                
                # Google Cast discovery
                print("    🔍 Scanning for Google Cast APIs...")
                cast_futures = {executor.submit(self._discover_google_cast, ip): ip for ip in network_ips}
                
                # Apple HomeKit discovery
                print("    🔍 Scanning for Apple HomeKit APIs...")
                homekit_futures = {executor.submit(self._discover_homekit, ip): ip for ip in network_ips}
                
                # Music Assistant discovery
                print("    🔍 Scanning for Music Assistant APIs...")
                ma_futures = {executor.submit(self._discover_music_assistant, ip): ip for ip in network_ips}
                
                # Process results with shorter timeouts
                lg_found = 0
                try:
                    for future in as_completed(lg_futures, timeout=15):  # Reduced timeout
                        ip = lg_futures[future]
                        try:
                            result = future.result(timeout=5)  # Individual result timeout
                            if result:
                                discovered[ip] = result
                                lg_found += 1
                                print(f"    ✅ LG TV found: {ip}")
                        except Exception:
                            pass
                except Exception:
                    print("    ⏰ LG TV discovery timed out")
                
                # Process Google Cast results
                cast_found = 0
                try:
                    for future in as_completed(cast_futures, timeout=15):  # Reduced timeout
                        ip = cast_futures[future]
                        try:
                            result = future.result(timeout=5)  # Individual result timeout
                            if result:
                                discovered[ip] = result
                                cast_found += 1
                                print(f"    ✅ Google Cast found: {ip}")
                        except Exception:
                            pass
                except Exception:
                    print("    ⏰ Google Cast discovery timed out")
                
                # Process HomeKit results
                homekit_found = 0
                try:
                    for future in as_completed(homekit_futures, timeout=15):  # Reduced timeout
                        ip = homekit_futures[future]
                        try:
                            result = future.result(timeout=5)  # Individual result timeout
                            if result:
                                discovered[ip] = result
                                homekit_found += 1
                                print(f"    ✅ HomeKit found: {ip}")
                        except Exception:
                            pass
                except Exception:
                    print("    ⏰ HomeKit discovery timed out")
                
                # Process Music Assistant results
                ma_found = 0
                try:
                    for future in as_completed(ma_futures, timeout=15):  # Reduced timeout
                        ip = ma_futures[future]
                        try:
                            result = future.result(timeout=5)  # Individual result timeout
                            if result:
                                discovered[ip] = result
                                ma_found += 1
                                print(f"    ✅ Music Assistant found: {ip}")
                        except Exception:
                            pass
                except Exception:
                    print("    ⏰ Music Assistant discovery timed out")
            
            print(f"  📊 API discovery results: LG TV={lg_found}, Cast={cast_found}, HomeKit={homekit_found}, MA={ma_found}")
            print(f"🔍 Device-specific API discovery found {len(discovered)} devices")
            return discovered
            
        except Exception as e:
            print(f"❌ Device-specific API discovery error: {e}")
            return {}
    
    def _discover_lg_tv(self, ip: str) -> Optional[Dict[str, Any]]:
        """Discover LG TV using WebOS API"""
        try:
            # Try WebOS API endpoints
            webos_endpoints = [
                "http://{}:3000/",
                "http://{}:3000/api/",
                "http://{}:3000/roap/api/",
                "http://{}:3000/roap/api/auth",
                "http://{}:3000/roap/api/command"
            ]
            
            for endpoint in webos_endpoints:
                try:
                    url = endpoint.format(ip)
                    response = requests.get(url, timeout=2)
                    
                    if response.status_code == 200:
                        content = response.text.lower()
                        if any(keyword in content for keyword in ["webos", "lg", "tv", "smart tv"]):
                            return {
                                "device_type": "smart_tv",
                                "manufacturer": "LG",
                                "model": "WebOS TV",
                                "services": ["LG_WebOS_API"],
                                "confidence": 0.9
                            }
                except Exception:
                    continue
            
            return None
            
        except Exception:
            return None
    
    def _discover_google_cast(self, ip: str) -> Optional[Dict[str, Any]]:
        """Discover Google Cast devices"""
        try:
            # Try Google Cast endpoints
            cast_endpoints = [
                "http://{}:8008/setup/eureka_info",
                "http://{}:8008/setup/assistant/state",
                "http://{}:8008/setup/device_info"
            ]
            
            for endpoint in cast_endpoints:
                try:
                    url = endpoint.format(ip)
                    response = requests.get(url, timeout=2)
                    
                    if response.status_code == 200:
                        content = response.text.lower()
                        if any(keyword in content for keyword in ["chromecast", "google cast", "cast", "nest"]):
                            if "nest" in content:
                                return {
                                    "device_type": "smart_speaker",
                                    "manufacturer": "Google",
                                    "model": "Nest",
                                    "services": ["Google_Cast_API"],
                                    "confidence": 0.9
                                }
                            else:
                                return {
                                    "device_type": "media_device",
                                    "manufacturer": "Google",
                                    "model": "Chromecast",
                                    "services": ["Google_Cast_API"],
                                    "confidence": 0.9
                                }
                except Exception:
                    continue
            
            return None
            
        except Exception:
            return None
    
    def _discover_homekit(self, ip: str) -> Optional[Dict[str, Any]]:
        """Discover Apple HomeKit devices"""
        try:
            # Try HomeKit endpoints
            homekit_endpoints = [
                "http://{}:51826/",
                "http://{}:51827/",
                "http://{}:51828/",
                "http://{}:51829/"
            ]
            
            for endpoint in homekit_endpoints:
                try:
                    url = endpoint.format(ip)
                    response = requests.get(url, timeout=2)
                    
                    if response.status_code == 200:
                        content = response.text.lower()
                        if any(keyword in content for keyword in ["homekit", "homepod", "apple", "airplay"]):
                            if "homepod" in content:
                                return {
                                    "device_type": "smart_speaker",
                                    "manufacturer": "Apple",
                                    "model": "HomePod",
                                    "services": ["HomeKit_API"],
                                    "confidence": 0.9
                                }
                            else:
                                return {
                                    "device_type": "homekit_device",
                                    "manufacturer": "Apple",
                                    "services": ["HomeKit_API"],
                                    "confidence": 0.8
                                }
                except Exception:
                    continue
            
            return None
            
        except Exception:
            return None
    
    def _discover_music_assistant(self, ip: str) -> Optional[Dict[str, Any]]:
        """Discover Music Assistant"""
        try:
            # Try Music Assistant endpoints
            ma_endpoints = [
                "http://{}:8095/",
                "http://{}:8095/api/",
                "http://{}:8095/api/v1/",
                "http://{}:8095/api/v1/providers"
            ]
            
            for endpoint in ma_endpoints:
                try:
                    url = endpoint.format(ip)
                    response = requests.get(url, timeout=2)
                    
                    if response.status_code == 200:
                        content = response.text.lower()
                        if any(keyword in content for keyword in ["music assistant", "musicassistant", "ma-"]):
                            return {
                                "device_type": "media_server",
                                "software": "Music Assistant",
                                "services": ["Music_Assistant_API"],
                                "confidence": 0.9
                            }
                except Exception:
                    continue
            
            return None
            
        except Exception:
            return None
    
    def discover_upnp_devices(self) -> Dict[str, Dict[str, Any]]:
        """Discover devices using UPnP/SSDP"""
        discovered = {}
        
        try:
            print("🔍 Starting UPnP/SSDP discovery...")
            
            # SSDP M-SEARCH message
            ssdp_message = (
                "M-SEARCH * HTTP/1.1\r\n"
                "HOST: 239.255.255.250:1900\r\n"
                "MAN: \"ssdp:discover\"\r\n"
                "MX: 3\r\n"
                "ST: ssdp:all\r\n"
                "\r\n"
            )
            
            # Create UDP socket for SSDP
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.settimeout(5)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
            
            # Send M-SEARCH to SSDP multicast address
            sock.sendto(ssdp_message.encode(), ("239.255.255.250", 1900))
            
            # Listen for responses
            start_time = time.time()
            while time.time() - start_time < 10:  # Listen for 10 seconds
                try:
                    data, addr = sock.recvfrom(1024)
                    response = data.decode('utf-8', errors='ignore')
                    
                    # Parse SSDP response
                    lines = response.split('\r\n')
                    location = None
                    server = None
                    usn = None
                    
                    for line in lines:
                        if line.startswith('LOCATION:'):
                            location = line.split(':', 1)[1].strip()
                        elif line.startswith('SERVER:'):
                            server = line.split(':', 1)[1].strip()
                        elif line.startswith('USN:'):
                            usn = line.split(':', 1)[1].strip()
                    
                    if location and addr[0] not in discovered:
                        discovered[addr[0]] = {
                            "hostname": None,
                            "services": ["UPnP"],
                            "upnp_location": location,
                            "upnp_server": server,
                            "upnp_usn": usn,
                            "device_type": "upnp_device",
                            "confidence": 0.7
                        }
                        
                        # Try to identify device type from USN or server
                        if usn:
                            usn_lower = usn.lower()
                            if "hue" in usn_lower:
                                discovered[addr[0]]["device_type"] = "philips_hue_bridge"
                                discovered[addr[0]]["manufacturer"] = "Philips"
                                discovered[addr[0]]["model"] = "Hue Bridge"
                            elif "sonos" in usn_lower:
                                discovered[addr[0]]["device_type"] = "speaker"
                                discovered[addr[0]]["manufacturer"] = "Sonos"
                            elif "plex" in usn_lower:
                                discovered[addr[0]]["device_type"] = "media_server"
                                discovered[addr[0]]["software"] = "Plex"
                            elif "printer" in usn_lower or "ipp" in usn_lower:
                                discovered[addr[0]]["device_type"] = "printer"
                            elif "router" in usn_lower or "gateway" in usn_lower:
                                discovered[addr[0]]["device_type"] = "router"
                            elif "tv" in usn_lower or "television" in usn_lower:
                                discovered[addr[0]]["device_type"] = "smart_tv"
                            elif "media" in usn_lower:
                                discovered[addr[0]]["device_type"] = "media_device"
                        
                except socket.timeout:
                    break
                except Exception:
                    continue
            
            sock.close()
            print(f"🔍 UPnP/SSDP discovery found {len(discovered)} devices")
            return discovered
            
        except Exception as e:
            print(f"❌ UPnP/SSDP discovery error: {e}")
            return {}
    
    def discover_dhcp_devices(self) -> Dict[str, Dict[str, Any]]:
        """Discover devices using DHCP fingerprinting and ARP table"""
        discovered = {}
        
        try:
            print("🔍 Starting DHCP/ARP discovery...")
            
            # Method 1: Try to get ARP table from /proc/net/arp (works on Linux)
            try:
                with open('/proc/net/arp', 'r') as f:
                    lines = f.readlines()
                    
                for line in lines[1:]:  # Skip header
                    if line.strip():
                        parts = line.split()
                        if len(parts) >= 4:
                            ip = parts[0]
                            mac = parts[3]
                            
                            # Skip incomplete entries (MAC address is 00:00:00:00:00:00)
                            if mac != "00:00:00:00:00:00" and ip not in discovered:
                                discovered[ip] = {
                                    "hostname": None,
                                    "mac_address": mac,
                                    "services": ["ARP"],
                                    "device_type": "arp_device",
                                    "confidence": 0.5
                                }
                                
                                # Try to get hostname for this IP
                                try:
                                    hostname = socket.gethostbyaddr(ip)[0]
                                    if hostname and hostname != ip:
                                        discovered[ip]["hostname"] = hostname
                                        
                                        # Try to identify device from hostname
                                        hostname_lower = hostname.lower()
                                        if "hue" in hostname_lower:
                                            discovered[ip]["device_type"] = "philips_hue_bridge"
                                            discovered[ip]["manufacturer"] = "Philips"
                                        elif "sonos" in hostname_lower:
                                            discovered[ip]["device_type"] = "speaker"
                                            discovered[ip]["manufacturer"] = "Sonos"
                                        elif "plex" in hostname_lower:
                                            discovered[ip]["device_type"] = "media_server"
                                            discovered[ip]["software"] = "Plex"
                                        elif "printer" in hostname_lower:
                                            discovered[ip]["device_type"] = "printer"
                                        elif "tv" in hostname_lower:
                                            discovered[ip]["device_type"] = "smart_tv"
                                        elif "router" in hostname_lower or "gateway" in hostname_lower:
                                            discovered[ip]["device_type"] = "router"
                                        elif "zero" in hostname_lower or "pi" in hostname_lower:
                                            discovered[ip]["device_type"] = "raspberry_pi"
                                            discovered[ip]["manufacturer"] = "Raspberry Pi"
                                except Exception:
                                    pass
            except Exception:
                pass
            
            # Method 2: Try common DHCP lease file locations
            dhcp_files = [
                "/var/lib/dhcp/dhcpd.leases",
                "/var/lib/misc/dnsmasq.leases", 
                "/tmp/dhcp.leases",
                "/etc/dhcp/dhcpd.leases",
                "/var/lib/dhcpcd/dhcpcd.leases"
            ]
            
            for dhcp_file in dhcp_files:
                try:
                    with open(dhcp_file, 'r') as f:
                        content = f.read()
                        lines = content.split('\n')
                        
                        for line in lines:
                            if line.strip() and not line.startswith('#'):
                                parts = line.split()
                                if len(parts) >= 4:
                                    timestamp = parts[0]
                                    mac = parts[1]
                                    ip = parts[2]
                                    hostname = parts[3] if len(parts) > 3 else None
                                    
                                    if ip not in discovered:
                                        discovered[ip] = {
                                            "hostname": hostname,
                                            "mac_address": mac,
                                            "dhcp_timestamp": timestamp,
                                            "services": ["DHCP"],
                                            "device_type": "dhcp_device",
                                            "confidence": 0.6
                                        }
                                        
                                        # Try to identify device from hostname
                                        if hostname:
                                            hostname_lower = hostname.lower()
                                            if "hue" in hostname_lower:
                                                discovered[ip]["device_type"] = "philips_hue_bridge"
                                                discovered[ip]["manufacturer"] = "Philips"
                                            elif "sonos" in hostname_lower:
                                                discovered[ip]["device_type"] = "speaker"
                                                discovered[ip]["manufacturer"] = "Sonos"
                                            elif "plex" in hostname_lower:
                                                discovered[ip]["device_type"] = "media_server"
                                                discovered[ip]["software"] = "Plex"
                                            elif "printer" in hostname_lower:
                                                discovered[ip]["device_type"] = "printer"
                                            elif "tv" in hostname_lower:
                                                discovered[ip]["device_type"] = "smart_tv"
                                            elif "router" in hostname_lower or "gateway" in hostname_lower:
                                                discovered[ip]["device_type"] = "router"
                                                
                except Exception:
                    continue
            
            print(f"🔍 DHCP/ARP discovery found {len(discovered)} devices")
            return discovered
            
        except Exception as e:
            print(f"❌ DHCP/ARP discovery error: {e}")
            return {}
    
    def identify_device_type(self, device: DiscoveredDevice) -> None:
        """Attempt to identify the type of device based on ports and services"""
        # Check for Jarvis nodes first
        if self._is_jarvis_node(device):
            device.is_jarvis_node = True
            device.device_type = "jarvis_node"
            device.confidence_score = 0.9
            return
        
        # Try HTTP fingerprinting for devices with web services
        if 80 in device.open_ports or 443 in device.open_ports:
            fingerprint = self.http_fingerprint_device(device.ip_address)
            if fingerprint["device_type"] and fingerprint["confidence"] > 0.7:
                device.device_type = fingerprint["device_type"]
                device.manufacturer = fingerprint.get("manufacturer")
                device.model = fingerprint.get("model")
                device.confidence_score = fingerprint["confidence"]
                return
        
        # Fallback to port-based detection
        if self._is_philips_hue(device):
            device.device_type = "philips_hue_bridge"
            device.manufacturer = "Philips"
            device.model = "Hue Bridge"
            device.confidence_score = 0.8
            return
        
        if self._is_smart_plug(device):
            device.device_type = "smart_plug"
            device.confidence_score = 0.7
            return
        
        if self._is_router(device):
            device.device_type = "router"
            device.confidence_score = 0.8
            return
        
        if self._is_printer(device):
            device.device_type = "printer"
            device.confidence_score = 0.7
            return
        
        # Check for Home Assistant systems
        if 8123 in device.open_ports:
            device.device_type = "home_assistant"
            device.confidence_score = 0.8
            return
        
        # Generic device classification
        if 80 in device.open_ports or 443 in device.open_ports:
            device.device_type = "web_device"
            device.confidence_score = 0.5
        elif 22 in device.open_ports:
            device.device_type = "linux_device"
            device.confidence_score = 0.6
        else:
            device.device_type = "unknown_device"
            device.confidence_score = 0.3
    
    def _is_jarvis_node(self, device: DiscoveredDevice) -> bool:
        """Check if device is likely a Jarvis node"""
        # Check hostname patterns (most reliable indicator)
        if device.hostname:
            hostname_lower = device.hostname.lower()
            for pattern in self.jarvis_identifiers["hostname_patterns"]:
                if pattern in hostname_lower:
                    return True
        
        # Check for specific Jarvis service combinations
        # A real Jarvis node should have MQTT + some web services
        has_mqtt = 1883 in device.open_ports
        has_web = any(port in device.open_ports for port in [80, 443, 8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090])
        
        # Only identify as Jarvis if it has both MQTT and web services
        # AND doesn't have Home Assistant (which would indicate it's a different system)
        if has_mqtt and has_web and 8123 not in device.open_ports:
            return True
        
        return False
    
    def _is_philips_hue(self, device: DiscoveredDevice) -> bool:
        """Check if device is a Philips Hue bridge"""
        # Hue bridges typically have both port 80 and 443 open
        # and are often at common IP addresses like .1
        if 80 in device.open_ports and 443 in device.open_ports:
            # Additional check: Hue bridges are often at .1 or have specific hostnames
            if device.ip_address.endswith('.1') or (device.hostname and 'hue' in device.hostname.lower()):
                return True
        return False
    
    def _is_smart_plug(self, device: DiscoveredDevice) -> bool:
        """Check if device is a smart plug"""
        # Smart plugs typically have SSH + web interface on non-standard ports
        # But not the full range of services a server would have
        has_ssh = 22 in device.open_ports
        web_ports = [8080, 8081, 8082, 8083, 8084, 8085, 8086, 8087, 8088, 8089, 8090]
        has_web = any(port in device.open_ports for port in web_ports)
        
        # Smart plugs usually have SSH + limited web ports, not full server services
        if has_ssh and has_web and len(device.open_ports) <= 5:
            return True
        return False
    
    def _is_router(self, device: DiscoveredDevice) -> bool:
        """Check if device is a router"""
        # Routers typically have port 80/443 open and are often at .1
        if device.ip_address.endswith('.1') and (80 in device.open_ports or 443 in device.open_ports):
            return True
        return False
    
    def _is_printer(self, device: DiscoveredDevice) -> bool:
        """Check if device is a printer"""
        # Printers often have port 631 (IPP) or 9100 (raw printing)
        if 631 in device.open_ports or 9100 in device.open_ports:
            return True
        return False
    
    def scan_device(self, ip: str) -> Optional[DiscoveredDevice]:
        """Scan a single device for open ports and services"""
        try:
            # Quick ping test first
            if not self.scan_port(ip, 80, timeout=0.5) and not self.scan_port(ip, 22, timeout=0.5):
                return None  # Skip devices that don't respond to common ports
            
            device = DiscoveredDevice(ip_address=ip)
            
            # Get hostname
            device.hostname = self.get_hostname(ip)
            
            # Get MAC address
            device.mac_address = self.get_mac_address(ip)
            
            # Scan common ports
            with ThreadPoolExecutor(max_workers=10) as executor:
                port_futures = {executor.submit(self.scan_port, ip, port): port 
                               for port in self.common_ports.keys()}
                
                for future in as_completed(port_futures, timeout=10):
                    port = port_futures[future]
                    try:
                        if future.result():
                            device.open_ports.append(port)
                            service_name = self.common_ports.get(port, f"Unknown-{port}")
                            device.services.append(service_name)
                    except Exception:
                        pass
            
            # Identify device type
            self.identify_device_type(device)
            
            return device if device.open_ports else None
            
        except Exception as e:
            print(f"Error scanning {ip}: {e}")
            return None
    
    def scan_network(self, force: bool = False, callback: Optional[Any] = None) -> Dict[str, DiscoveredDevice]:
        """Scan the network for devices using multiple discovery methods"""
        with self.scan_lock:
            # Don't scan if already in progress or recently scanned
            if self.scan_in_progress:
                return self.discovered_devices.copy()
            
            if not force and time.time() - self.last_scan_time < 300:  # 5 minutes
                return self.discovered_devices.copy()
            
            self.scan_in_progress = True
        
        try:
            print("🔍 Starting comprehensive network discovery scan...")
            print("📋 Discovery methods: mDNS, UPnP/SSDP, DHCP/ARP, Device APIs, HTTP Fingerprinting")
            print("⏱️  Estimated time: 2-3 minutes")
            print()
            
            # Run all discovery methods in parallel
            print("🚀 Starting parallel discovery methods...")
            with ThreadPoolExecutor(max_workers=4) as executor:
                # Start all discovery methods
                print("  🔍 Starting mDNS discovery...")
                mdns_future = executor.submit(self.discover_mdns_services)
                
                print("  🔍 Starting UPnP/SSDP discovery...")
                upnp_future = executor.submit(self.discover_upnp_devices)
                
                print("  🔍 Starting DHCP/ARP discovery...")
                dhcp_future = executor.submit(self.discover_dhcp_devices)
                
                print("  🔍 Starting device-specific API discovery...")
                api_future = executor.submit(self.discover_device_specific_apis)
                
                # Get results
                print("⏳ Waiting for discovery methods to complete...")
                mdns_devices = mdns_future.result()
                print(f"✅ mDNS discovery complete: {len(mdns_devices)} devices")
                
                upnp_devices = upnp_future.result()
                print(f"✅ UPnP/SSDP discovery complete: {len(upnp_devices)} devices")
                
                dhcp_devices = dhcp_future.result()
                print(f"✅ DHCP/ARP discovery complete: {len(dhcp_devices)} devices")
                
                api_devices = api_future.result()
                print(f"✅ Device API discovery complete: {len(api_devices)} devices")
            
            # Merge all discovered devices
            print("🔄 Merging discovery results...")
            all_discovered = {}
            
            # Add mDNS devices
            print(f"  📱 Adding {len(mdns_devices)} mDNS devices...")
            for ip, device_info in mdns_devices.items():
                if ip not in all_discovered:
                    all_discovered[ip] = DiscoveredDevice(ip_address=ip)
                
                device = all_discovered[ip]
                device.hostname = device_info.get("hostname")
                device.device_type = device_info.get("device_type", "mdns_device")
                device.manufacturer = device_info.get("manufacturer")
                device.model = device_info.get("model")
                device.services.extend(device_info.get("services", []))
                device.confidence_score = max(device.confidence_score, 0.8)
            
            # Add UPnP devices
            print(f"  📱 Adding {len(upnp_devices)} UPnP devices...")
            for ip, device_info in upnp_devices.items():
                if ip not in all_discovered:
                    all_discovered[ip] = DiscoveredDevice(ip_address=ip)
                
                device = all_discovered[ip]
                device.hostname = device_info.get("hostname")
                device.device_type = device_info.get("device_type", "upnp_device")
                device.manufacturer = device_info.get("manufacturer")
                device.model = device_info.get("model")
                device.services.extend(device_info.get("services", []))
                device.confidence_score = max(device.confidence_score, 0.7)
            
            # Add DHCP devices
            print(f"  📱 Adding {len(dhcp_devices)} DHCP/ARP devices...")
            for ip, device_info in dhcp_devices.items():
                if ip not in all_discovered:
                    all_discovered[ip] = DiscoveredDevice(ip_address=ip)
                
                device = all_discovered[ip]
                device.hostname = device_info.get("hostname")
                device.device_type = device_info.get("device_type", "dhcp_device")
                device.manufacturer = device_info.get("manufacturer")
                device.model = device_info.get("model")
                device.mac_address = device_info.get("mac_address")
                device.services.extend(device_info.get("services", []))
                device.confidence_score = max(device.confidence_score, 0.6)
            
            # Add device-specific API devices
            print(f"  📱 Adding {len(api_devices)} API-discovered devices...")
            for ip, device_info in api_devices.items():
                if ip not in all_discovered:
                    all_discovered[ip] = DiscoveredDevice(ip_address=ip)
                
                device = all_discovered[ip]
                device.hostname = device_info.get("hostname")
                device.device_type = device_info.get("device_type", "api_device")
                device.manufacturer = device_info.get("manufacturer")
                device.model = device_info.get("model")
                device.services.extend(device_info.get("services", []))
                device.confidence_score = max(device.confidence_score, 0.9)
            
            print(f"✅ Merged {len(all_discovered)} total devices")
            print()
            
            # Now do traditional port scanning for devices we found
            print("🔍 Starting port scanning phase...")
            network_ips = self.get_network_range()
            smart_devices_found = 0
            
            print(f"  🔍 Scanning {len(network_ips)} IP addresses for open ports...")
            with ThreadPoolExecutor(max_workers=20) as executor:
                
                future_to_ip = {executor.submit(self.scan_device, ip): ip for ip in network_ips}
                
                completed = 0
                for future in as_completed(future_to_ip, timeout=120):
                    ip = future_to_ip[future]
                    completed += 1
                    
                    # Show progress every 25 devices
                    if completed % 25 == 0:
                        print(f"  📊 Port scanning progress: {completed}/{len(network_ips)} ({completed/len(network_ips)*100:.1f}%)")
                    
                    try:
                        device = future.result()
                        if device:
                            if ip in all_discovered:
                                # Merge with existing device info
                                existing = all_discovered[ip]
                                existing.open_ports.extend(device.open_ports)
                                existing.services.extend(device.services)
                                existing.confidence_score = max(existing.confidence_score, device.confidence_score)
                                
                                # Use better device type if available
                                if device.device_type != "unknown_device":
                                    existing.device_type = device.device_type
                                if device.manufacturer:
                                    existing.manufacturer = device.manufacturer
                                if device.model:
                                    existing.model = device.model
                                
                                # Re-identify device type with all info
                                self.identify_device_type(existing)
                            else:
                                all_discovered[ip] = device
                            
                            # Check if it's a smart device
                            is_smart = (not device.is_jarvis_node and 
                                       device.device_type in ["philips_hue_bridge", "smart_plug", "router", "printer", "web_device", "home_assistant", "media_server", "smart_speaker", "airplay_device", "media_device", "management_interface", "security_camera", "smart_thermostat"])
                            
                            if is_smart:
                                smart_devices_found += 1
                                print(f"💡 Smart device #{smart_devices_found}: {device.ip_address} - {device.device_type}")
                                if device.hostname:
                                    print(f"    Hostname: {device.hostname}")
                                if device.manufacturer:
                                    print(f"    Manufacturer: {device.manufacturer}")
                                if device.model:
                                    print(f"    Model: {device.model}")
                                print(f"    Services: {', '.join(device.services)}")
                                print()
                            else:
                                print(f"✅ Found device: {device.ip_address} ({device.device_type})")
                            
                            # Call callback if provided
                            if callback:
                                callback(device, all_discovered)
                                
                    except Exception as e:
                        print(f"❌ Error scanning {ip}: {e}")
            
            print(f"✅ Port scanning complete: {completed}/{len(network_ips)} devices scanned")
            print()
            
            # Now add HTTP fingerprinting to devices that don't have specific device types
            print("🔍 Starting HTTP fingerprinting phase...")
            devices_to_fingerprint = []
            
            for ip, device in all_discovered.items():
                # Skip devices that already have good identification
                if (device.device_type and device.device_type not in ["arp_device", "dhcp_device", "mdns_device", "upnp_device", "unknown_device"]):
                    continue
                
                # Add to fingerprinting list (including devices without open ports)
                devices_to_fingerprint.append(device)
            
            print(f"  🔍 HTTP fingerprinting {len(devices_to_fingerprint)} unidentified devices...")
            
            with ThreadPoolExecutor(max_workers=5) as executor:
                future_to_device = {executor.submit(self._fingerprint_device, device): device for device in devices_to_fingerprint}
                
                completed_fingerprints = 0
                for future in as_completed(future_to_device, timeout=120):
                    device = future_to_device[future]
                    completed_fingerprints += 1
                    
                    # Show progress every 5 devices
                    if completed_fingerprints % 5 == 0:
                        print(f"  📊 HTTP fingerprinting progress: {completed_fingerprints}/{len(devices_to_fingerprint)} ({completed_fingerprints/len(devices_to_fingerprint)*100:.1f}%)")
                    
                    try:
                        fingerprint_result = future.result()
                        if fingerprint_result and fingerprint_result.get("device_type"):
                            # Update device with fingerprint results
                            device.device_type = fingerprint_result["device_type"]
                            device.manufacturer = fingerprint_result.get("manufacturer")
                            device.model = fingerprint_result.get("model")
                            device.confidence_score = max(device.confidence_score, fingerprint_result.get("confidence", 0.0))
                            
                            # Add HTTP fingerprinting to services
                            if "HTTP_Fingerprinting" not in device.services:
                                device.services.append("HTTP_Fingerprinting")
                            
                            print(f"🔍 HTTP Fingerprint: {device.ip_address} → {device.device_type}")
                            if device.manufacturer:
                                print(f"    Manufacturer: {device.manufacturer}")
                            if device.model:
                                print(f"    Model: {device.model}")
                            
                            # Check if this is now a smart device
                            is_smart = (not device.is_jarvis_node and 
                                       device.device_type in ["philips_hue_bridge", "smart_plug", "router", "printer", "web_device", "home_assistant", "media_server", "smart_speaker", "airplay_device", "media_device", "management_interface", "security_camera", "smart_thermostat"])
                            
                            if is_smart:
                                smart_devices_found += 1
                                print(f"💡 Smart device #{smart_devices_found}: {device.ip_address} - {device.device_type}")
                                print()
                                
                    except Exception as e:
                        print(f"❌ Error fingerprinting {device.ip_address}: {e}")
            
            print(f"✅ HTTP fingerprinting complete: {completed_fingerprints}/{len(devices_to_fingerprint)} devices processed")
            print()
            
            self.discovered_devices = all_discovered
            self.last_scan_time = time.time()
            
            print("🎉 Comprehensive network scan complete!")
            print("=" * 60)
            print(f"📊 Total devices found: {len(all_discovered)}")
            print(f"💡 Smart devices found: {smart_devices_found}")
            print(f"🔍 Discovery methods used: mDNS, UPnP/SSDP, DHCP, Port Scanning, Device APIs, HTTP Fingerprinting")
            print("=" * 60)
            return all_discovered
            
        finally:
            self.scan_in_progress = False
    
    def get_jarvis_nodes(self) -> List[DiscoveredDevice]:
        """Get list of discovered Jarvis nodes"""
        return [device for device in self.discovered_devices.values() if device.is_jarvis_node]
    
    def get_smart_devices(self) -> List[DiscoveredDevice]:
        """Get list of discovered smart devices (non-Jarvis)"""
        return [device for device in self.discovered_devices.values() 
                if not device.is_jarvis_node and 
                device.device_type in ["philips_hue_bridge", "smart_plug", "router", "printer", "web_device", "home_assistant", "media_server", "smart_speaker", "airplay_device", "media_device", "management_interface", "security_camera", "smart_thermostat", "smart_tv"]]
    
    def get_discovery_summary(self) -> Dict[str, Any]:
        """Get a summary of discovered devices"""
        jarvis_nodes = self.get_jarvis_nodes()
        smart_devices = self.get_smart_devices()
        
        return {
            "total_devices": len(self.discovered_devices),
            "jarvis_nodes": len(jarvis_nodes),
            "smart_devices": len(smart_devices),
            "last_scan": self.last_scan_time,
            "scan_in_progress": self.scan_in_progress,
            "devices": {
                "jarvis_nodes": [device.to_dict() for device in jarvis_nodes],
                "smart_devices": [device.to_dict() for device in smart_devices],
                "other_devices": [device.to_dict() for device in self.discovered_devices.values() 
                                if device not in jarvis_nodes and device not in smart_devices]
            }
        }
    
    def save_discovery_results_to_json(self, filename: str = "discovered_devices.json") -> str:
        """Save discovered devices to a JSON file for inspection"""
        import json
        from datetime import datetime
        
        try:
            # Create a comprehensive discovery report
            discovery_report = {
                "scan_timestamp": datetime.now().isoformat(),
                "scan_summary": self.get_discovery_summary(),
                "all_devices": {
                    ip: device.to_dict() for ip, device in self.discovered_devices.items()
                },
                "raw_discovery_data": {
                    "mdns_discovered": self.mdns_discovered,
                    "upnp_discovered": self.upnp_discovered,
                    "dhcp_discovered": self.dhcp_discovered
                }
            }
            
            # Save to file
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(discovery_report, f, indent=2, ensure_ascii=False)
            
            print(f"💾 Discovery results saved to: {filename}")
            print(f"📊 File contains {len(self.discovered_devices)} devices")
            return filename
            
        except Exception as e:
            print(f"❌ Error saving discovery results to JSON: {e}")
            return ""


# Global instance
_discovery_service = None

def get_network_discovery_service() -> NetworkDiscoveryService:
    """Get the global network discovery service instance"""
    global _discovery_service
    if _discovery_service is None:
        _discovery_service = NetworkDiscoveryService()
    return _discovery_service 