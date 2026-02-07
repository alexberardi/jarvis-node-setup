"""
Pydantic models for the provisioning API.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class ProvisioningState(str, Enum):
    """States for the provisioning state machine."""
    AP_MODE = "AP_MODE"           # Broadcasting AP, waiting for connection
    CONNECTING = "CONNECTING"     # Attempting to connect to home WiFi
    REGISTERING = "REGISTERING"   # Registering with command center
    PROVISIONED = "PROVISIONED"   # Successfully provisioned
    ERROR = "ERROR"               # Error occurred


class NodeInfo(BaseModel):
    """Response for GET /api/v1/info - node identification and capabilities."""
    node_id: str = Field(..., description="Unique node identifier (e.g., jarvis-a1b2c3d4)")
    firmware_version: str = Field(..., description="Node software version")
    hardware: str = Field(..., description="Hardware type (e.g., pi-zero-w, ubuntu)")
    mac_address: str = Field(..., description="Network MAC address")
    capabilities: list[str] = Field(default_factory=list, description="Node capabilities")
    state: ProvisioningState = Field(..., description="Current provisioning state")


class NetworkInfo(BaseModel):
    """WiFi network scan result."""
    ssid: str = Field(..., description="Network SSID")
    signal_strength: int = Field(..., description="Signal strength in dBm (e.g., -45)")
    security: str = Field(..., description="Security type (e.g., WPA2, WPA3, OPEN)")


class ScanNetworksResponse(BaseModel):
    """Response for GET /api/v1/scan-networks."""
    networks: list[NetworkInfo] = Field(default_factory=list)


class ProvisionRequest(BaseModel):
    """Request for POST /api/v1/provision - WiFi credentials and setup info."""
    wifi_ssid: str = Field(..., description="Home WiFi network SSID")
    wifi_password: str = Field(..., description="Home WiFi network password")
    room: str = Field(..., description="Room name for this node (e.g., kitchen, bedroom)")
    command_center_url: str = Field(..., description="URL of the command center (e.g., http://192.168.1.50:8002)")
    household_id: str = Field(..., description="UUID of the household this node belongs to")
    admin_key: Optional[str] = Field(default=None, description="Admin API key for command center registration")


class ProvisionResponse(BaseModel):
    """Response for POST /api/v1/provision."""
    success: bool = Field(..., description="Whether credentials were accepted")
    message: str = Field(..., description="Status message")


class ProvisionStatus(BaseModel):
    """Response for GET /api/v1/status - current provisioning progress."""
    state: ProvisioningState = Field(..., description="Current provisioning state")
    message: str = Field(..., description="Human-readable status message")
    progress_percent: int = Field(default=0, ge=0, le=100, description="Progress percentage")
    error: Optional[str] = Field(default=None, description="Error message if state is ERROR")


class K2ProvisionRequest(BaseModel):
    """Request for POST /api/v1/provision/k2 - mobile provides K2 encryption key."""
    node_id: str = Field(..., description="Node ID to provision K2 for")
    kid: str = Field(..., description="Key identifier (e.g., k2-2026-01)")
    k2: str = Field(..., description="Base64url-encoded 32-byte K2 key")
    created_at: datetime = Field(..., description="When the key was created")


class K2ProvisionResponse(BaseModel):
    """Response for POST /api/v1/provision/k2."""
    success: bool = Field(..., description="Whether K2 was accepted and stored")
    node_id: Optional[str] = Field(default=None, description="Node ID if successful")
    kid: Optional[str] = Field(default=None, description="Key ID if successful")
    error: Optional[str] = Field(default=None, description="Error message if unsuccessful")
