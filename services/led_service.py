"""LEDService — Pi Zero ACT LED control for alert notification.

Patterns:
- "off"    — LED off
- "normal" — restore default kernel control
- "alert"  — slow blink (~1Hz)

On non-Pi platforms (macOS, Docker), all operations are no-ops with logging.
"""

import platform
import threading
import time
from pathlib import Path
from typing import Optional

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

_LED_TRIGGER = Path("/sys/class/leds/ACT/trigger")
_LED_BRIGHTNESS = Path("/sys/class/leds/ACT/brightness")


class LEDService:
    """Control the Pi Zero ACT LED blink pattern."""

    def __init__(self) -> None:
        self._pattern: str = "normal"
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._is_pi = self._detect_pi()

        if not self._is_pi:
            logger.debug("LED service: non-Pi platform, running in no-op mode")

    @staticmethod
    def _detect_pi() -> bool:
        """Detect if running on a Raspberry Pi with writable LED sysfs."""
        if platform.system() != "Linux":
            return False
        return _LED_TRIGGER.exists() and _LED_BRIGHTNESS.exists()

    def set_pattern(self, pattern: str) -> None:
        """Set the LED blink pattern: 'off', 'normal', or 'alert'."""
        if pattern == self._pattern:
            return

        old = self._pattern
        self._pattern = pattern
        logger.debug("LED pattern changed", old=old, new=pattern)

        if not self._is_pi:
            return

        # Stop existing blink thread
        self._stop_blink_thread()

        if pattern == "off":
            self._write_led("none", "0")
        elif pattern == "normal":
            self._write_led("default-on", None)
        elif pattern == "alert":
            self._write_led("none", None)
            self._start_blink_thread()

    def cleanup(self) -> None:
        """Restore default LED trigger on shutdown."""
        self._stop_blink_thread()
        if self._is_pi:
            self._write_led("default-on", None)
        logger.debug("LED service cleaned up")

    def _write_led(self, trigger: str, brightness: Optional[str]) -> None:
        """Write to LED sysfs files."""
        try:
            _LED_TRIGGER.write_text(trigger)
            if brightness is not None:
                _LED_BRIGHTNESS.write_text(brightness)
        except (PermissionError, OSError) as e:
            logger.warning("Cannot write LED sysfs", error=str(e))

    def _start_blink_thread(self) -> None:
        """Start a daemon thread that blinks the LED ~1Hz."""
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._blink_loop, daemon=True)
        self._thread.start()

    def _stop_blink_thread(self) -> None:
        """Stop the blink thread if running."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _blink_loop(self) -> None:
        """Toggle LED brightness at ~1Hz until stopped."""
        state = False
        while not self._stop_event.is_set():
            state = not state
            try:
                _LED_BRIGHTNESS.write_text("1" if state else "0")
            except (PermissionError, OSError):
                break
            self._stop_event.wait(0.5)

    @property
    def current_pattern(self) -> str:
        return self._pattern


# Singleton
_instance: Optional[LEDService] = None


def get_led_service() -> LEDService:
    global _instance
    if _instance is None:
        _instance = LEDService()
    return _instance
