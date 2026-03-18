"""Abstract interface for device listing backends.

A DeviceManager collects the full list of devices from a backend
(Home Assistant, Jarvis Direct, etc.) and returns them in a normalized
format.  Unlike IJarvisDeviceProtocol (which handles per-protocol discovery +
control), a DeviceManager aggregates across protocols or delegates to
an external system.

Implementations live in ``device_managers/`` and are auto-discovered
by ``DeviceManagerDiscoveryService``.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from core.ijarvis_authentication import AuthenticationConfig
from core.ijarvis_secret import IJarvisSecret


@dataclass
class DeviceManagerDevice:
    """Normalized device returned by any device manager."""

    name: str
    domain: str  # "light", "switch", "lock", "climate", etc.
    entity_id: str  # "{domain}.{slug}"
    is_controllable: bool = True
    manufacturer: str | None = None
    model: str | None = None
    protocol: str | None = None  # "lifx", "kasa", "home_assistant"
    local_ip: str | None = None
    mac_address: str | None = None
    cloud_id: str | None = None
    device_class: str | None = None
    source: str = "direct"  # "home_assistant" or "direct"
    area: str | None = None  # Room/area name from the source
    state: str | None = None  # "on", "off", "unavailable"
    extra: dict[str, Any] = field(default_factory=dict)


class IJarvisDeviceManager(ABC):
    """Interface for device listing backends.

    Each implementation wraps one source of device truth (Home Assistant,
    Jarvis Direct protocol adapters, etc.).  The node selects the active
    manager based on a CC setting and reports its device list back to CC.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier (e.g., 'home_assistant', 'jarvis_direct')."""
        ...

    @property
    @abstractmethod
    def friendly_name(self) -> str:
        """Human-readable display name (e.g., 'Home Assistant')."""
        ...

    @property
    def description(self) -> str:
        """Short description for mobile settings UI."""
        return ""

    @property
    @abstractmethod
    def can_edit_devices(self) -> bool:
        """Whether mobile should show an edit UI for the device list.

        True for Jarvis Direct (user curates), False for HA (HA is source of truth).
        """
        ...

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        """Secrets needed for this manager to function."""
        return []

    @property
    def authentication(self) -> AuthenticationConfig | None:
        """OAuth or other auth config, if any."""
        return None

    def is_available(self) -> bool:
        """Check whether all required secrets are present.

        Returns True when every required secret has a non-empty value.
        """
        from services.secret_service import get_secret_value

        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                return False
        return True

    def validate_secrets(self) -> list[str]:
        """Return list of missing required secret keys."""
        from services.secret_service import get_secret_value

        missing: list[str] = []
        for secret in self.required_secrets:
            if secret.required and not get_secret_value(secret.key, secret.scope):
                missing.append(secret.key)
        return missing

    @abstractmethod
    async def collect_devices(self) -> list[DeviceManagerDevice]:
        """Collect the full device list from this backend.

        Returns:
            Normalized list of devices.
        """
        ...
