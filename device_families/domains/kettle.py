"""Kettle domain handler (smart kettles with temperature control)."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


class KettleDomainHandler(DomainHandler):
    """Handler for smart kettle devices."""

    @property
    def domain(self) -> str:
        return "kettle"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(name="turn_on", display_name="Turn On"),
            DomainAction(name="turn_off", display_name="Turn Off"),
            DomainAction(
                name="set_temperature",
                display_name="Set Temperature",
                params=["temperature"],
            ),
            DomainAction(
                name="set_mode",
                display_name="Set Mode",
                params=["mode"],
            ),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize kettle state from Govee or other adapter format."""
        state: dict[str, Any] = {"online": raw.get("online", True)}

        # Power state
        power = raw.get("state", "off")
        state["state"] = str(power).lower()

        # Current water temperature
        current = raw.get("current_temperature")
        if current is not None:
            state["current_temperature"] = round(float(current))

        # Target temperature
        target = raw.get("target_temperature")
        if target is not None:
            state["target_temperature"] = round(float(target))

        # Temperature unit
        unit = raw.get("unit", "C")
        state["unit"] = unit.upper() if isinstance(unit, str) else "C"

        # Work mode (boil, keep_warm, etc.)
        mode = raw.get("mode") or raw.get("work_mode")
        if mode is not None:
            state["mode"] = str(mode).lower()

        return state

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        return UIControlHints(
            control_type="kettle",
            features=features or ["boil", "keep_warm", "off"],
            min_value=40,
            max_value=100,
            step=1,
            unit="C",
        )
