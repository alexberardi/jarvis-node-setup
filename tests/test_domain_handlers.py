"""Tests for device_families.domains handlers."""

from unittest.mock import patch

import pytest

from device_families.domains import get_domain_handler
from device_families.domains.base import UIControlHints
from device_families.domains.camera import CameraDomainHandler
from device_families.domains.climate import ClimateDomainHandler
from device_families.domains.cover import CoverDomainHandler
from device_families.domains.light import LightDomainHandler
from device_families.domains.lock import LockDomainHandler
from device_families.domains.media_player import MediaPlayerDomainHandler
from device_families.domains.switch import SwitchDomainHandler


class TestRegistry:
    def test_get_known_domains(self) -> None:
        for domain in ("climate", "light", "switch", "lock", "camera", "cover", "media_player"):
            assert get_domain_handler(domain) is not None

    def test_get_unknown_domain_returns_none(self) -> None:
        assert get_domain_handler("nonexistent") is None


class TestClimateDomainHandler:
    @pytest.fixture()
    def handler(self) -> ClimateDomainHandler:
        return ClimateDomainHandler()

    def test_domain(self, handler: ClimateDomainHandler) -> None:
        assert handler.domain == "climate"

    def test_resolve_canonical_action(self, handler: ClimateDomainHandler) -> None:
        action = handler.resolve_action("set_temperature")
        assert action is not None
        assert action.name == "set_temperature"

    def test_resolve_alias_set_hvac_mode(self, handler: ClimateDomainHandler) -> None:
        action = handler.resolve_action("set_hvac_mode")
        assert action is not None
        assert action.name == "set_mode"

    def test_resolve_unknown_action(self, handler: ClimateDomainHandler) -> None:
        assert handler.resolve_action("explode") is None

    @patch("device_families.domains.climate._get_temp_unit", return_value="F")
    def test_normalize_state_ha_style(self, _mock: object, handler: ClimateDomainHandler) -> None:
        raw = {
            "hvac_action": "heating",
            "hvac_mode": "heat",
            "current_temperature": 71.0,
            "temperature": 72.0,
            "humidity": 45,
        }
        state = handler.normalize_state(raw)
        assert state["state"] == "heating"
        assert state["mode"] == "heat"
        assert state["current_temperature"] == 71
        assert state["target_temperature"] == 72
        assert state["humidity"] == 45
        assert state["unit"] == "F"

    @patch("device_families.domains.climate._get_temp_unit", return_value="F")
    def test_normalize_state_nest_adapter_style(self, _mock: object, handler: ClimateDomainHandler) -> None:
        raw = {
            "state": "HEATING",
            "mode": "HEAT",
            "current_temperature_f": 71.0,
            "target_temperature_f": 72.0,
            "humidity": 45,
            "online": True,
        }
        state = handler.normalize_state(raw)
        assert state["state"] == "heating"
        assert state["mode"] == "heat"
        assert state["current_temperature"] == 71
        assert state["target_temperature"] == 72
        assert state["online"] is True

    @patch("device_families.domains.climate._get_temp_unit", return_value="C")
    def test_normalize_state_celsius(self, _mock: object, handler: ClimateDomainHandler) -> None:
        raw = {
            "current_temperature_c": 21.5,
            "target_temperature_c": 22.0,
        }
        state = handler.normalize_state(raw)
        assert state["current_temperature"] == 22
        assert state["target_temperature"] == 22

    @patch("device_families.domains.climate._get_temp_unit", return_value="F")
    def test_ui_hints_fahrenheit(self, _mock: object, handler: ClimateDomainHandler) -> None:
        hints = handler.get_ui_hints()
        assert hints.control_type == "thermostat"
        assert hints.min_value == 50
        assert hints.max_value == 90
        assert hints.unit == "F"

    @patch("device_families.domains.climate._get_temp_unit", return_value="C")
    def test_ui_hints_celsius(self, _mock: object, handler: ClimateDomainHandler) -> None:
        hints = handler.get_ui_hints()
        assert hints.min_value == 10
        assert hints.max_value == 32
        assert hints.unit == "C"

    @patch("device_families.domains.climate._get_temp_unit", return_value="F")
    def test_normalize_state_nest_sdm_style(self, _mock: object, handler: ClimateDomainHandler) -> None:
        """Nest SDM adapter returns current_temperature, target_temperature, and setpoints."""
        raw = {
            "state": "on",
            "mode": "HEAT",
            "current_temperature": 72.5,
            "target_temperature": 70.0,
            "heat_setpoint": 70.0,
            "humidity": 45,
            "available_modes": ["HEAT", "COOL", "HEATCOOL", "OFF"],
        }
        state = handler.normalize_state(raw)
        assert state["state"] == "on"
        assert state["mode"] == "heat"
        assert state["current_temperature"] == 72  # round(72.5) → 72 (banker's rounding)
        assert state["target_temperature"] == 70
        assert state["humidity"] == 45

    @patch("device_families.domains.climate._get_temp_unit", return_value="F")
    def test_normalize_state_nest_cool_mode(self, _mock: object, handler: ClimateDomainHandler) -> None:
        """In cool mode, target_temperature should come from cool_setpoint."""
        raw = {
            "state": "on",
            "mode": "COOL",
            "current_temperature": 78.0,
            "heat_setpoint": 68.0,
            "cool_setpoint": 75.0,
            "humidity": 50,
        }
        state = handler.normalize_state(raw)
        assert state["mode"] == "cool"
        assert state["current_temperature"] == 78
        assert state["target_temperature"] == 75

    @patch("device_families.domains.climate._get_temp_unit", return_value="F")
    def test_ui_hints_custom_features(self, _mock: object, handler: ClimateDomainHandler) -> None:
        hints = handler.get_ui_hints(features=["heat", "off"])
        assert hints.features == ["heat", "off"]


class TestLightDomainHandler:
    @pytest.fixture()
    def handler(self) -> LightDomainHandler:
        return LightDomainHandler()

    def test_domain(self, handler: LightDomainHandler) -> None:
        assert handler.domain == "light"

    def test_resolve_set_brightness_alias(self, handler: LightDomainHandler) -> None:
        action = handler.resolve_action("brightness")
        assert action is not None
        assert action.name == "set_brightness"

    def test_normalize_state_ha_brightness(self, handler: LightDomainHandler) -> None:
        raw = {"state": "on", "brightness": 255}
        state = handler.normalize_state(raw)
        assert state["state"] == "on"
        assert state["brightness"] == 100

    def test_normalize_state_adapter_brightness(self, handler: LightDomainHandler) -> None:
        raw = {"state": "on", "brightness": 75}
        state = handler.normalize_state(raw)
        assert state["brightness"] == 75

    def test_normalize_state_off(self, handler: LightDomainHandler) -> None:
        raw = {"state": "off"}
        state = handler.normalize_state(raw)
        assert state["state"] == "off"
        assert "brightness" not in state

    def test_normalize_state_with_rgb(self, handler: LightDomainHandler) -> None:
        raw = {"state": "on", "rgb": [255, 0, 128]}
        state = handler.normalize_state(raw)
        assert state["rgb"] == [255, 0, 128]

    def test_normalize_state_ha_hs_color(self, handler: LightDomainHandler) -> None:
        raw = {"state": "on", "hs_color": [180, 75]}
        state = handler.normalize_state(raw)
        assert state["hue"] == 180
        assert state["saturation"] == 75

    def test_normalize_state_color_temp(self, handler: LightDomainHandler) -> None:
        raw = {"state": "on", "color_temp": 3500}
        state = handler.normalize_state(raw)
        assert state["color_temp"] == 3500

    def test_resolve_set_color(self, handler: LightDomainHandler) -> None:
        action = handler.resolve_action("set_color")
        assert action is not None
        assert action.name == "set_color"

    def test_resolve_color_alias(self, handler: LightDomainHandler) -> None:
        action = handler.resolve_action("color")
        assert action is not None
        assert action.name == "set_color"

    def test_ui_hints(self, handler: LightDomainHandler) -> None:
        hints = handler.get_ui_hints()
        assert hints.control_type == "light"
        assert hints.max_value == 100
        assert "color" in hints.features


class TestSwitchDomainHandler:
    @pytest.fixture()
    def handler(self) -> SwitchDomainHandler:
        return SwitchDomainHandler()

    def test_domain(self, handler: SwitchDomainHandler) -> None:
        assert handler.domain == "switch"

    def test_normalize_state(self, handler: SwitchDomainHandler) -> None:
        assert handler.normalize_state({"state": "ON"}) == {"state": "on"}
        assert handler.normalize_state({}) == {"state": "off"}

    def test_ui_hints(self, handler: SwitchDomainHandler) -> None:
        assert handler.get_ui_hints().control_type == "toggle"

    def test_all_actions(self, handler: SwitchDomainHandler) -> None:
        names = [a.name for a in handler.canonical_actions]
        assert "turn_on" in names
        assert "turn_off" in names
        assert "toggle" in names


class TestLockDomainHandler:
    @pytest.fixture()
    def handler(self) -> LockDomainHandler:
        return LockDomainHandler()

    def test_normalize_locked(self, handler: LockDomainHandler) -> None:
        state = handler.normalize_state({"state": "locked"})
        assert state["is_locked"] is True

    def test_normalize_unlocked(self, handler: LockDomainHandler) -> None:
        state = handler.normalize_state({"state": "unlocked"})
        assert state["is_locked"] is False

    def test_ui_hints(self, handler: LockDomainHandler) -> None:
        assert handler.get_ui_hints().control_type == "lock"


class TestCameraDomainHandler:
    @pytest.fixture()
    def handler(self) -> CameraDomainHandler:
        return CameraDomainHandler()

    def test_normalize_online(self, handler: CameraDomainHandler) -> None:
        state = handler.normalize_state({"online": True})
        assert state["state"] == "online"
        assert state["online"] is True

    def test_normalize_offline_from_state(self, handler: CameraDomainHandler) -> None:
        state = handler.normalize_state({"state": "offline"})
        assert state["state"] == "offline"
        assert state["online"] is False

    def test_resolve_get_stream(self, handler: CameraDomainHandler) -> None:
        action = handler.resolve_action("get_stream")
        assert action is not None

    def test_ui_hints(self, handler: CameraDomainHandler) -> None:
        assert handler.get_ui_hints().control_type == "camera"


class TestCoverDomainHandler:
    @pytest.fixture()
    def handler(self) -> CoverDomainHandler:
        return CoverDomainHandler()

    def test_normalize_with_position(self, handler: CoverDomainHandler) -> None:
        state = handler.normalize_state({"state": "open", "current_position": 75})
        assert state["state"] == "open"
        assert state["position"] == 75

    def test_normalize_closed(self, handler: CoverDomainHandler) -> None:
        state = handler.normalize_state({"state": "closed"})
        assert state["state"] == "closed"
        assert "position" not in state

    def test_resolve_alias_open(self, handler: CoverDomainHandler) -> None:
        action = handler.resolve_action("open")
        assert action is not None
        assert action.name == "open_cover"

    def test_ui_hints(self, handler: CoverDomainHandler) -> None:
        hints = handler.get_ui_hints()
        assert hints.control_type == "cover"


class TestMediaPlayerDomainHandler:
    @pytest.fixture()
    def handler(self) -> MediaPlayerDomainHandler:
        return MediaPlayerDomainHandler()

    def test_normalize_playing(self, handler: MediaPlayerDomainHandler) -> None:
        raw = {"state": "playing", "volume_level": 0.65, "media_title": "Song"}
        state = handler.normalize_state(raw)
        assert state["state"] == "playing"
        assert state["volume"] == 65
        assert state["media_title"] == "Song"

    def test_normalize_off(self, handler: MediaPlayerDomainHandler) -> None:
        state = handler.normalize_state({"state": "off"})
        assert state["state"] == "off"
        assert "volume" not in state

    def test_ui_hints(self, handler: MediaPlayerDomainHandler) -> None:
        assert handler.get_ui_hints().control_type == "media"
