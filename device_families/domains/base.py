"""Base domain handler interface and data structures.

Domain handlers normalize device actions and state across different protocols
(HA, Nest, Govee, LIFX, Kasa) into a canonical shape per domain. They also
provide UI hints so the mobile app can render per-domain controls.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DomainAction:
    """A canonical action for a device domain."""

    name: str  # "set_temperature" (canonical)
    display_name: str  # "Set Temperature"
    params: list[str] = field(default_factory=list)  # Required data keys
    aliases: list[str] = field(default_factory=list)  # HA service names that map here


@dataclass
class UIControlHints:
    """Metadata for the mobile app to render domain-specific controls."""

    control_type: str  # "thermostat", "light", "toggle", "lock", "cover", "camera", "media"
    features: list[str] = field(default_factory=list)  # e.g., ["heat","cool","off"]
    min_value: float | None = None
    max_value: float | None = None
    step: float | None = None
    unit: str | None = None


class DomainHandler(ABC):
    """Base class for device domain handlers.

    Each handler normalizes raw state from any protocol adapter into a
    canonical shape, resolves action names (including HA aliases), and
    provides UI hints for the mobile app.
    """

    @property
    @abstractmethod
    def domain(self) -> str:
        """Domain name (e.g., 'climate', 'light')."""
        ...

    @property
    @abstractmethod
    def canonical_actions(self) -> list[DomainAction]:
        """List of canonical actions for this domain."""
        ...

    @abstractmethod
    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize raw adapter/HA state into canonical shape.

        Args:
            raw: Raw state dict from adapter.get_state() or HA entity state.

        Returns:
            Normalized state dict with domain-specific canonical keys.
        """
        ...

    @abstractmethod
    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        """Get UI rendering hints for this domain.

        Args:
            features: Optional feature list to customize hints
                (e.g., which HVAC modes are available).

        Returns:
            UIControlHints for the mobile app.
        """
        ...

    def resolve_action(self, action: str) -> DomainAction | None:
        """Resolve an action name (canonical or alias) to a DomainAction.

        Args:
            action: Action name to resolve (e.g., "set_hvac_mode" or "set_mode").

        Returns:
            Matching DomainAction, or None if not found.
        """
        for da in self.canonical_actions:
            if action == da.name or action in da.aliases:
                return da
        return None
