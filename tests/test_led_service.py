"""Tests for LEDService."""

from unittest.mock import patch

from services.led_service import LEDService
from services.respeaker_led_service import _PATTERNS, NUM_LEDS


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

        led._stable = "alert"  # simulate previous state
        with patch.object(led, "_write_led") as mock_write:
            with patch.object(led, "_stop_blink_thread"):
                led.set_pattern("normal")
                mock_write.assert_called_once_with("default-on", None)

    def test_transient_overlay_then_clear(self) -> None:
        """Transient pattern overlays stable; clearing reverts."""
        with patch.object(LEDService, "_detect_pi", return_value=False):
            led = LEDService()

        assert led.current_pattern == "normal"
        led.set_transient_pattern("listening")
        assert led.current_pattern == "listening"
        led.set_pattern("alert")
        assert led.current_pattern == "listening"  # transient still wins
        led.set_transient_pattern(None)
        assert led.current_pattern == "alert"  # back to stable

    def test_error_blinks_distinctly(self) -> None:
        """Error transient gets a distinct blink rate on the monochrome ACT LED."""
        with patch.object(LEDService, "_detect_pi", return_value=True):
            led = LEDService()

        with patch.object(led, "_write_led"), patch.object(led, "_start_blink_thread"):
            led.set_transient_pattern("error")
            assert led._blink_period == 0.15  # ~3Hz — between alert (1Hz) and shutdown (5Hz)

    def test_wake_detected_falls_through_on_act(self) -> None:
        """wake_detected is RGB-only — on ACT LED it falls through to stable."""
        with patch.object(LEDService, "_detect_pi", return_value=False):
            led = LEDService()
        led.set_transient_pattern("wake_detected")
        assert led._effective_pattern() == "normal"


class TestRespeakerPatterns:
    """Test the pure pattern-rendering functions for the RGB ReSpeaker HAT."""

    def test_new_patterns_registered(self) -> None:
        assert "wake_detected" in _PATTERNS
        assert "error" in _PATTERNS

    def test_wake_detected_is_purple(self) -> None:
        pixels = _PATTERNS["wake_detected"](0.0)
        assert len(pixels) == NUM_LEDS
        for r, g, b, brt in pixels:
            assert (r, g, b) == (180, 0, 255)
            assert brt > 0

    def test_listening_is_steady_green(self) -> None:
        """Listening should be steady (same on every frame) and green."""
        frame_a = _PATTERNS["listening"](0.0)
        frame_b = _PATTERNS["listening"](0.5)
        frame_c = _PATTERNS["listening"](1.5)
        assert frame_a == frame_b == frame_c  # steady
        for r, g, b, _brt in frame_a:
            assert g > r and g > b  # green dominant

    def test_error_is_steady_red(self) -> None:
        pixels = _PATTERNS["error"](0.0)
        for r, g, b, brt in pixels:
            assert (r, g, b) == (255, 0, 0)
            assert brt > 0

    def test_thinking_pinwheels_one_two_three(self) -> None:
        """Thinking cycles count of lit LEDs: 1 → 2 → 3 → 1 → 2 → 3 …

        Phase advances every 1/3s (3Hz), so we sample within each phase
        window and count how many LEDs have non-zero brightness.
        """
        def lit_count(t: float) -> int:
            return sum(1 for (_r, _g, _b, brt) in _PATTERNS["thinking"](t) if brt > 0)

        # Within the first second, phases 0/1/2 → 1/2/3 LEDs lit.
        assert lit_count(0.05) == 1
        assert lit_count(0.40) == 2
        assert lit_count(0.70) == 3
        # Loops back: second cycle, same pattern.
        assert lit_count(1.05) == 1
        assert lit_count(1.40) == 2
        assert lit_count(1.70) == 3

    def test_thinking_is_amber(self) -> None:
        """Lit LEDs in the pinwheel use amber (R>G>B, no blue)."""
        pixels = _PATTERNS["thinking"](0.70)  # all 3 lit
        for r, g, b, brt in pixels:
            if brt > 0:
                assert r > g > b
                assert b == 0
