"""Camera domain handler."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


class CameraDomainHandler(DomainHandler):
    """Handler for camera/doorbell devices."""

    @property
    def domain(self) -> str:
        return "camera"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(
                name="get_stream",
                display_name="Get Stream",
                aliases=["generate_stream"],
            ),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        online = raw.get("online", raw.get("state") != "offline")
        return {
            "state": "online" if online else "offline",
            "online": bool(online),
        }

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        return UIControlHints(control_type="camera", features=features or [])
