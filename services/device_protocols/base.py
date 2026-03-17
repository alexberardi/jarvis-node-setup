"""Base protocol adapter interface for direct WiFi device control."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DiscoveredDevice:
    """A device found during LAN scanning."""

    name: str
    domain: str  # "light", "switch", "fan", etc.
    manufacturer: str
    model: str
    protocol: str  # "lifx", "kasa", "tuya", etc.
    local_ip: str
    mac_address: str
    entity_id: str  # Generated: "{domain}.{slug}" e.g. "light.living_room_lifx"
    device_class: str | None = None
    is_controllable: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceControlResult:
    """Result of a direct device control operation."""

    success: bool
    entity_id: str
    action: str
    error: str | None = None


class DeviceProtocol(ABC):
    """Interface for manufacturer-specific LAN device protocols.

    Each implementation handles discovery and control for one protocol
    (e.g., LIFX LAN, TP-Link Kasa local API, Tuya local keys).
    """

    @property
    @abstractmethod
    def protocol_name(self) -> str:
        """Short protocol identifier (e.g., 'lifx', 'kasa')."""
        ...

    @property
    @abstractmethod
    def supported_domains(self) -> list[str]:
        """HA-style domains this protocol can control (e.g., ['light', 'switch'])."""
        ...

    @abstractmethod
    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        """Scan the LAN for devices using this protocol.

        Args:
            timeout: How long to listen for responses (seconds).

        Returns:
            List of discovered devices.
        """
        ...

    @abstractmethod
    async def control(
        self,
        ip: str,
        action: str,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DeviceControlResult:
        """Send a control command to a device.

        Args:
            ip: Device's LAN IP address.
            action: Action name (e.g., "turn_on", "turn_off", "toggle").
            data: Optional action-specific data (e.g., {"brightness": 50}).
            **kwargs: Protocol-specific arguments (e.g., mac_address for LIFX).

        Returns:
            Result of the control operation.
        """
        ...

    @abstractmethod
    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        """Query current device state.

        Args:
            ip: Device's LAN IP address.
            **kwargs: Protocol-specific arguments.

        Returns:
            State dict with at least {"state": "on"|"off"} or None on failure.
        """
        ...
