"""Climate domain handler (thermostats, HVAC)."""

from typing import Any

from device_families.domains.base import DomainAction, DomainHandler, UIControlHints


def _get_temp_unit() -> str:
    """Read NEST_TEMP_UNIT from secrets, default to F."""
    try:
        from services.secret_service import get_secret_value

        unit = get_secret_value("NEST_TEMP_UNIT", "integration")
        return unit.upper() if unit and unit.upper() in ("F", "C") else "F"
    except Exception:
        return "F"


class ClimateDomainHandler(DomainHandler):
    """Handler for climate/thermostat devices."""

    @property
    def domain(self) -> str:
        return "climate"

    @property
    def canonical_actions(self) -> list[DomainAction]:
        return [
            DomainAction(
                name="set_temperature",
                display_name="Set Temperature",
                params=["temperature"],
                aliases=["set_temp"],
            ),
            DomainAction(
                name="set_mode",
                display_name="Set Mode",
                params=["mode"],
                aliases=["set_hvac_mode"],
            ),
            DomainAction(name="turn_on", display_name="Turn On"),
            DomainAction(name="turn_off", display_name="Turn Off"),
        ]

    def normalize_state(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Normalize thermostat state from HA or adapter format."""
        unit = _get_temp_unit()
        state: dict[str, Any] = {"unit": unit, "online": raw.get("online", True)}

        # HVAC state: HA uses "hvac_action", Nest adapter uses "state"
        hvac_state = raw.get("hvac_action") or raw.get("state") or "off"
        state["state"] = str(hvac_state).lower()

        # Mode: HA uses "hvac_mode" attr or top-level, Nest uses "mode"
        mode = raw.get("hvac_mode") or raw.get("mode") or "off"
        state["mode"] = str(mode).lower()

        # Current temperature
        current = raw.get("current_temperature")
        if current is None:
            # Nest adapter returns separate F/C keys
            if unit == "F":
                current = raw.get("current_temperature_f")
            else:
                current = raw.get("current_temperature_c")
        if current is not None:
            state["current_temperature"] = round(float(current))

        # Target temperature: prefer explicit target, then setpoints, then HA "temperature"
        target = raw.get("target_temperature")
        if target is None:
            # Nest adapter: use heat/cool setpoint based on mode
            mode_lower = str(mode).lower()
            if mode_lower == "cool" and "cool_setpoint" in raw:
                target = raw.get("cool_setpoint")
            elif "heat_setpoint" in raw:
                target = raw.get("heat_setpoint")
        if target is None:
            # HA format: "temperature" is the target (only when current_temperature
            # is already set separately, so we don't confuse ambient with target)
            if "current_temperature" in raw or "current_temperature" in state:
                target = raw.get("temperature")
            elif raw.get("temperature") is not None and current is None:
                # No current temp available — "temperature" is likely HA target
                target = raw.get("temperature")
        if target is None:
            if unit == "F":
                target = raw.get("target_temperature_f")
            else:
                target = raw.get("target_temperature_c")
        if target is not None:
            state["target_temperature"] = round(float(target))

        # Humidity
        humidity = raw.get("humidity")
        if humidity is not None:
            state["humidity"] = round(float(humidity))

        return state

    def get_ui_hints(self, features: list[str] | None = None) -> UIControlHints:
        unit = _get_temp_unit()
        return UIControlHints(
            control_type="thermostat",
            features=features or ["heat", "cool", "off"],
            min_value=50 if unit == "F" else 10,
            max_value=90 if unit == "F" else 32,
            step=1,
            unit=unit,
        )
