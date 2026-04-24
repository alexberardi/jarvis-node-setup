"""Tests for utils.mic_device.resolve_input_device_index."""

from unittest.mock import MagicMock, patch

from utils.mic_device import resolve_input_device_index


def _make_pa(devices: list[tuple[str, int]]) -> MagicMock:
    """Build a mock PyAudio instance from (name, max_input_channels) tuples."""
    pa = MagicMock()
    pa.get_device_count.return_value = len(devices)
    pa.get_device_info_by_index.side_effect = lambda i: {
        "name": devices[i][0],
        "maxInputChannels": devices[i][1],
    }
    return pa


def _config_stub(name: str | None = None, index: str | None = None):
    def _get_str(key: str, default=None):
        if key == "mic_device_name":
            return name
        if key == "mic_device_index":
            return index
        return default
    return patch("utils.mic_device.Config.get_str", side_effect=_get_str)


class TestResolveInputDeviceIndex:
    def test_name_match_picks_matching_input_device(self) -> None:
        pa = _make_pa([
            ("HDMI 1", 0),
            ("USB PnP Sound Device", 1),
            ("dsnoopmic", 1),
        ])
        with _config_stub(name="dsnoopmic"):
            assert resolve_input_device_index(pa) == 2

    def test_name_match_ignores_output_only_devices(self) -> None:
        # A device whose name matches but has no input channels must not be picked.
        pa = _make_pa([
            ("dsnoopmic-out", 0),
            ("dsnoopmic", 1),
        ])
        with _config_stub(name="dsnoop"):
            assert resolve_input_device_index(pa) == 1

    def test_falls_back_to_index_when_name_has_no_match(self) -> None:
        pa = _make_pa([
            ("HDMI", 0),
            ("USB PnP", 1),
        ])
        with _config_stub(name="nonexistent", index="1"):
            assert resolve_input_device_index(pa) == 1

    def test_rejects_index_pointing_at_output_device(self) -> None:
        # Index 0 is output-only (HifiBerry) — resolver should skip to auto-fallback.
        pa = _make_pa([
            ("HifiBerry DAC", 0),
            ("USB PnP", 1),
        ])
        with _config_stub(index="0"):
            assert resolve_input_device_index(pa) == 1

    def test_auto_selects_first_input_when_nothing_configured(self) -> None:
        pa = _make_pa([
            ("HDMI", 0),
            ("USB PnP", 1),
            ("dsnoopmic", 1),
        ])
        with _config_stub():
            assert resolve_input_device_index(pa) == 1

    def test_returns_none_when_no_input_devices_exist(self) -> None:
        pa = _make_pa([
            ("HDMI", 0),
            ("HifiBerry DAC", 0),
        ])
        with _config_stub():
            assert resolve_input_device_index(pa) is None
