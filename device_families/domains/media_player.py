"""Media player domain handler."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


class MediaPlayerDomainHandler(DomainHandler):
    """Handler for media player devices."""

    @property
    def domain(self) -> str:
        return "media_player"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(name="turn_on", display_name="Turn On"),
            DomainAction(name="turn_off", display_name="Turn Off"),
            DomainAction(name="toggle", display_name="Toggle"),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        state = str(raw.get("state", "off")).lower()
        result: dict[str, Any] = {"state": state}

        volume = raw.get("volume_level")
        if volume is not None:
            result["volume"] = round(float(volume) * 100)

        for key in ("media_title", "media_artist", "source"):
            if raw.get(key):
                result[key] = raw[key]

        return result

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        return UIControlHints(control_type="media", features=features or [])
