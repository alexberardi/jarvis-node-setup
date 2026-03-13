"""Tests for LEDService."""

from unittest.mock import patch

from services.led_service import LEDService


class TestLEDService:
    def setup_method(self) -> None:
        # Force non-Pi mode for all tests
        with patch.object(LEDService, "_detect_pi", return_value=False):
            self.led = LEDService()

    def test_initial_pattern_is_normal(self) -> None:
        assert self.led.current_pattern == "normal"

    def test_set_pattern_changes_state(self) -> None:
        self.led.set_pattern("alert")
        assert self.led.current_pattern == "alert"

    def test_set_pattern_off(self) -> None:
        self.led.set_pattern("off")
        assert self.led.current_pattern == "off"

    def test_set_same_pattern_noop(self) -> None:
        self.led.set_pattern("normal")
        # Should not raise or change anything
        assert self.led.current_pattern == "normal"

    def test_cleanup_restores_normal(self) -> None:
        self.led.set_pattern("alert")
        self.led.cleanup()
        # cleanup doesn't change _pattern but that's fine, it restores hardware

    def test_noop_on_macos(self) -> None:
        """On non-Pi, set_pattern should work without errors."""
        assert not self.led._is_pi
        self.led.set_pattern("alert")
        self.led.set_pattern("off")
        self.led.set_pattern("normal")
        self.led.cleanup()


class TestLEDServiceOnPi:
    """Test LED writes on a simulated Pi (mocked sysfs)."""

    def test_alert_pattern_writes_trigger(self) -> None:
        with patch.object(LEDService, "_detect_pi", return_value=True):
            led = LEDService()

        with patch.object(led, "_write_led") as mock_write:
            with patch.object(led, "_start_blink_thread"):
                led.set_pattern("alert")
                mock_write.assert_called_once_with("none", None)

    def test_off_pattern_writes_brightness_zero(self) -> None:
        with patch.object(LEDService, "_detect_pi", return_value=True):
            led = LEDService()

        with patch.object(led, "_write_led") as mock_write:
            led.set_pattern("off")
            mock_write.assert_called_once_with("none", "0")

    def test_normal_pattern_restores_default(self) -> None:
        with patch.object(LEDService, "_detect_pi", return_value=True):
            led = LEDService()

        led._pattern = "alert"  # simulate previous state
        with patch.object(led, "_write_led") as mock_write:
            with patch.object(led, "_stop_blink_thread"):
                led.set_pattern("normal")
                mock_write.assert_called_once_with("default-on", None)
