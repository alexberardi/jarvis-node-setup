"""Lock domain handler."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


class LockDomainHandler(DomainHandler):
    """Handler for lock devices."""

    @property
    def domain(self) -> str:
        return "lock"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(name="lock", display_name="Lock"),
            DomainAction(name="unlock", display_name="Unlock"),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        state = str(raw.get("state", "unknown")).lower()
        return {
            "state": state,
            "is_locked": state == "locked",
        }

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        return UIControlHints(control_type="lock", features=features or [])
