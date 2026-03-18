"""Domain handler registry.

Provides canonical action resolution, state normalization, and UI hints
for each device domain (climate, light, switch, lock, camera, cover, media_player).
"""

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints
from device_families.domains.camera import CameraDomainHandler
from device_families.domains.climate import ClimateDomainHandler
from device_families.domains.cover import CoverDomainHandler
from device_families.domains.kettle import KettleDomainHandler
from device_families.domains.light import LightDomainHandler
from device_families.domains.lock import LockDomainHandler
from device_families.domains.media_player import MediaPlayerDomainHandler
from device_families.domains.switch import SwitchDomainHandler

_HANDLERS: dict[str, DomainHandler] = {
    "climate": ClimateDomainHandler(),
    "light": LightDomainHandler(),
    "switch": SwitchDomainHandler(),
    "lock": LockDomainHandler(),
    "camera": CameraDomainHandler(),
    "cover": CoverDomainHandler(),
    "media_player": MediaPlayerDomainHandler(),
    "kettle": KettleDomainHandler(),
}


def get_domain_handler(domain: str) -> DomainHandler | None:
    """Get the domain handler for a given domain name.

    Args:
        domain: Device domain (e.g., "climate", "light").

    Returns:
        DomainHandler instance or None if no handler for this domain.
    """
    return _HANDLERS.get(domain)


__all__ = [
    "DomainAction",
    "DomainHandler",
    "UIControlHints",
    "get_domain_handler",
]
