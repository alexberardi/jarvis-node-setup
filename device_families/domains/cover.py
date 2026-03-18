"""Cover domain handler (garage doors, blinds, shades)."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


class CoverDomainHandler(DomainHandler):
    """Handler for cover devices."""

    @property
    def domain(self) -> str:
        return "cover"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(name="open_cover", display_name="Open", aliases=["open"]),
            DomainAction(name="close_cover", display_name="Close", aliases=["close"]),
            DomainAction(name="stop_cover", display_name="Stop", aliases=["stop"]),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        state = str(raw.get("state", "unknown")).lower()
        result: dict[str, Any] = {"state": state}

        position = raw.get("current_position")
        if position is not None:
            result["position"] = round(float(position))

        return result

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        return UIControlHints(
            control_type="cover",
            features=features or [],
            min_value=0,
            max_value=100,
            step=1,
            unit="%",
        )
