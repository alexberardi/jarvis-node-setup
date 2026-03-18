"""Switch domain handler."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


class SwitchDomainHandler(DomainHandler):
    """Handler for switch/plug devices."""

    @property
    def domain(self) -> str:
        return "switch"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(name="turn_on", display_name="Turn On"),
            DomainAction(name="turn_off", display_name="Turn Off"),
            DomainAction(name="toggle", display_name="Toggle"),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        return {"state": str(raw.get("state", "off")).lower()}

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        return UIControlHints(control_type="toggle", features=features or [])
