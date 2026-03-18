"""Light domain handler."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


class LightDomainHandler(DomainHandler):
    """Handler for light devices."""

    @property
    def domain(self) -> str:
        return "light"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(name="turn_on", display_name="Turn On"),
            DomainAction(name="turn_off", display_name="Turn Off"),
            DomainAction(name="toggle", display_name="Toggle"),
            DomainAction(
                name="set_brightness",
                display_name="Set Brightness",
                params=["brightness"],
                aliases=["brightness"],
            ),
            DomainAction(
                name="set_color",
                display_name="Set Color",
                params=["rgb"],
                aliases=["color"],
            ),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        state_val = str(raw.get("state", "off")).lower()
        result: dict[str, Any] = {"state": state_val}

        # Brightness: HA uses 0-255, adapters use 0-100
        brightness = raw.get("brightness")
        if brightness is not None:
            b = float(brightness)
            # HA returns 0-255, normalize to 0-100
            if b > 100:
                b = round(b / 255 * 100)
            result["brightness"] = round(b)

        # Color: normalize to RGB [r,g,b] 0-255
        rgb = raw.get("rgb") or raw.get("rgb_color")
        if rgb is not None and isinstance(rgb, (list, tuple)) and len(rgb) == 3:
            result["rgb"] = [int(c) for c in rgb]

        # Hue/saturation (0-360, 0-100)
        hue = raw.get("hue")
        saturation = raw.get("saturation")
        if hue is not None:
            result["hue"] = round(float(hue))
        if saturation is not None:
            result["saturation"] = round(float(saturation))

        # HA hs_color: [hue 0-360, saturation 0-100]
        hs = raw.get("hs_color")
        if hs is not None and isinstance(hs, (list, tuple)) and len(hs) == 2:
            if "hue" not in result:
                result["hue"] = round(float(hs[0]))
            if "saturation" not in result:
                result["saturation"] = round(float(hs[1]))

        # Color temperature (kelvin)
        color_temp = raw.get("color_temp") or raw.get("color_temp_kelvin")
        if color_temp is not None:
            result["color_temp"] = int(color_temp)

        return result

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        return UIControlHints(
            control_type="light",
            features=features or ["brightness", "color", "color_temp"],
            min_value=0,
            max_value=100,
            step=1,
            unit="%",
        )
