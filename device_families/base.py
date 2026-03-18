"""Base protocol adapter interface for smart home device control.

Supports LAN-only, cloud-only, and hybrid (LAN + cloud) device families.
Each family implements discovery and control for one manufacturer/protocol.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal

from core.ijarvis_authentication import AuthenticationConfig
from core.ijarvis_button import IJarvisButton
from core.ijarvis_secret import IJarvisSecret


@dataclass
class DiscoveredDevice:
    """A device found during scanning (LAN or cloud)."""

    name: str
    domain: str  # "light", "switch", "fan", "lock", "climate", etc.
    manufacturer: str
    model: str
    protocol: str  # "lifx", "kasa", "govee", etc.
    entity_id: str  # Generated: "{domain}.{slug}" e.g. "light.living_room_lifx"
    local_ip: str | None = None
    mac_address: str | None = None
    cloud_id: str | None = None
    device_class: str | None = None
    is_controllable: bool = True
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class DeviceControlResult:
    """Result of a device control operation."""

    success: bool
    entity_id: str
    action: str
    error: str | None = None


class IJarvisDeviceProtocol(ABC):
    """Interface for manufacturer-specific device protocols.

    Each implementation handles discovery and control for one protocol
    (e.g., LIFX LAN, TP-Link Kasa local API, Govee cloud API).

    Families with missing required secrets are skipped during discovery
    (not errored), similar to IJarvisAgent behavior.
    """

    @property
    @abstractmethod
    def protocol_name(self) -> str:
        """Short protocol identifier (e.g., 'lifx', 'kasa', 'govee')."""
        ...

    @property
    @abstractmethod
    def supported_domains(self) -> list[str]:
        """HA-style domains this protocol can control (e.g., ['light', 'switch'])."""
        ...

    @property
    def connection_type(self) -> Literal["lan", "cloud", "hybrid"]:
        """How this family connects to devices.

        - "lan": Local network only (LIFX, Kasa)
        - "cloud": Cloud API only (Schlage, Nest)
        - "hybrid": Both LAN and cloud (Govee)

        Defaults to "lan" for backwards compatibility.
        """
        return "lan"

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        """Secrets this family needs to function (e.g., API keys).

        Defaults to empty list (no secrets required).
        """
        return []

    @property
    def friendly_name(self) -> str:
        """Human-readable display name (e.g., 'LIFX', 'TP-Link Kasa').

        Defaults to title-cased protocol_name with underscores replaced by spaces.
        """
        return self.protocol_name.replace("_", " ").title()

    @property
    def description(self) -> str:
        """Short description for the mobile UI.

        Defaults to empty string.
        """
        return ""

    @property
    def authentication(self) -> AuthenticationConfig | None:
        """OAuth or other auth config for this device family.

        Defaults to None (no auth needed). Override for cloud families
        that require OAuth (Schlage, Nest, etc.).
        """
        return None

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        """Default device control buttons. Override for protocol-specific actions."""
        return [
            IJarvisButton("Turn On", "turn_on", "primary", "power"),
            IJarvisButton("Turn Off", "turn_off", "secondary", "power-off"),
        ]

    def store_auth_values(self, values: dict[str, str]) -> None:
        """Store auth tokens/values received from the mobile app's OAuth flow.

        Called by config_push_service when an auth:* push matches this family.
        Override in families that use OAuth.

        Args:
            values: Key-value pairs from the OAuth token exchange.
        """

    def validate_secrets(self) -> list[str]:
        """Check which required secrets are missing.

        Returns:
            List of missing secret keys (empty if all present).
        """
        from services.secret_service import get_secret_value

        missing: list[str] = []
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                missing.append(secret.key)
        return missing

    @abstractmethod
    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        """Scan for devices using this protocol.

        For LAN protocols, this broadcasts on the local network.
        For cloud protocols, this queries the manufacturer's API.

        Args:
            timeout: How long to wait for responses (seconds).

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
            ip: Device's LAN IP address (may be empty for cloud-only devices).
            action: Action name (e.g., "turn_on", "turn_off", "toggle").
            data: Optional action-specific data (e.g., {"brightness": 50}).
            **kwargs: Protocol-specific arguments (e.g., mac_address, cloud_id).

        Returns:
            Result of the control operation.
        """
        ...

    @abstractmethod
    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        """Query current device state.

        Args:
            ip: Device's LAN IP address (may be empty for cloud-only devices).
            **kwargs: Protocol-specific arguments.

        Returns:
            State dict with at least {"state": "on"|"off"} or None on failure.
        """
        ...
