"""LEDService — Pi Zero ACT LED control for alert notification.

Two-layer state model:

- ``set_pattern(name)`` updates the *stable* pattern (idle/alert), driven
  by long-running state like the alert queue.
- ``set_transient_pattern(name | None)`` overlays a transient command-flow
  state (wake_detected / listening / thinking / speaking / error /
  shutdown_warning); ``None`` clears the overlay so the stable pattern
  resumes.

On the monochrome ACT LED only ``off``, ``normal``, ``alert``, ``error``,
and ``shutdown_warning`` have distinct visuals — other transients fall
back to the stable pattern (the ReSpeaker RGB service in
``respeaker_led_service.py`` renders the full set when available).

``get_led_service()`` returns the ReSpeaker RGB service when an APA102
chain is detected on SPI, otherwise the ACT-LED implementation here.

On non-Pi platforms (macOS, Docker), all operations are no-ops.
"""

import platform
import threading
from pathlib import Path
from typing import Optional, Union

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

_LED_TRIGGER = Path("/sys/class/leds/ACT/trigger")
_LED_BRIGHTNESS = Path("/sys/class/leds/ACT/brightness")


class LEDService:
    """Control the Pi Zero ACT LED."""

    def __init__(self) -> None:
        self._stable: str = "normal"
        self._transient: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._blink_period: float = 0.5
        self._enabled: bool = True
        self._is_pi = self._detect_pi()

        if not self._is_pi:
            logger.debug("LED service: non-Pi platform, running in no-op mode")

    @staticmethod
    def _detect_pi() -> bool:
        if platform.system() != "Linux":
            return False
        return _LED_TRIGGER.exists() and _LED_BRIGHTNESS.exists()

    def set_pattern(self, pattern: str) -> None:
        """Set the stable LED pattern: 'off', 'normal', or 'alert'."""
        if pattern == self._stable:
            return
        old = self._stable
        self._stable = pattern
        logger.debug("LED stable pattern changed", old=old, new=pattern)
        self._reapply()

    def set_transient_pattern(self, pattern: Optional[str]) -> None:
        """Overlay a transient pattern, or None to clear.

        Most transient patterns (wake_detected/listening/thinking/
        speaking/muted) are no-ops on the monochrome ACT LED — they
        fall through to the stable pattern. ``error`` blinks at ~3Hz
        for visibility; ``shutdown_warning`` triggers a fast 5Hz blink
        while held.
        """
        if pattern == self._transient:
            return
        old = self._transient
        self._transient = pattern
        logger.debug("LED transient pattern changed", old=old, new=pattern)
        self._reapply()

    def preview_pattern(self, pattern: str, duration_seconds: float = 3.0) -> None:
        """Show ``pattern`` briefly then revert. Monochrome fallback: best-effort.

        On the ACT LED only off/alert/shutdown_warning have distinct
        visuals, so previewing e.g. "thinking" looks the same as "normal".
        We still drive the transient + auto-clear so the API is uniform
        across hardware variants.
        """
        self.set_transient_pattern(pattern)
        t = threading.Timer(
            duration_seconds, lambda: self.set_transient_pattern(None)
        )
        t.daemon = True
        t.start()

    def set_enabled(self, enabled: bool) -> None:
        """When disabled, force the ACT LED off regardless of pattern."""
        self._enabled = bool(enabled)
        self._reapply()

    def set_brightness_scale(self, percent: int) -> None:
        """No-op on the monochrome ACT LED — accepted for API parity."""
        return None

    @property
    def current_pattern(self) -> str:
        return self._transient or self._stable

    def cleanup(self) -> None:
        """Restore default LED trigger on shutdown."""
        self._stop_blink_thread()
        if self._is_pi:
            self._write_led("default-on", None)
        logger.debug("LED service cleaned up")

    def _reapply(self) -> None:
        if not self._is_pi:
            return

        active = self._effective_pattern()
        self._stop_blink_thread()

        if not self._enabled:
            self._write_led("none", "0")
            return

        if active == "off":
            self._write_led("none", "0")
        elif active == "alert":
            self._write_led("none", None)
            self._blink_period = 0.5
            self._start_blink_thread()
        elif active == "error":
            self._write_led("none", None)
            self._blink_period = 0.15  # ~3Hz, distinct from alert (1Hz) + shutdown (5Hz)
            self._start_blink_thread()
        elif active == "shutdown_warning":
            self._write_led("none", None)
            self._blink_period = 0.1
            self._start_blink_thread()
        else:
            # normal + transient command-flow states (wake_detected/listening/
            # thinking/speaking/muted) all map to kernel default on a
            # monochrome LED — RGB visuals belong to respeaker_led_service.
            self._write_led("default-on", None)

    def _effective_pattern(self) -> str:
        # ACT LED can only meaningfully render a small subset; remap the
        # transient command-flow states to "normal" so they don't disrupt
        # the visual when no RGB is present. error/shutdown_warning still
        # render distinctly because they're safety-relevant signals.
        if self._transient in (
            None, "wake_detected", "listening", "thinking", "speaking", "not_for_me", "muted",
        ):
            return self._stable
        return self._transient

    def _write_led(self, trigger: str, brightness: Optional[str]) -> None:
        try:
            _LED_TRIGGER.write_text(trigger)
            if brightness is not None:
                _LED_BRIGHTNESS.write_text(brightness)
        except (PermissionError, OSError) as e:
            logger.warning("Cannot write LED sysfs", error=str(e))

    def _start_blink_thread(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._blink_loop, daemon=True)
        self._thread.start()

    def _stop_blink_thread(self) -> None:
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        self._thread = None

    def _blink_loop(self) -> None:
        state = False
        while not self._stop_event.is_set():
            state = not state
            try:
                _LED_BRIGHTNESS.write_text("1" if state else "0")
            except (PermissionError, OSError):
                break
            self._stop_event.wait(self._blink_period)


_LEDInstance = Union["LEDService", "object"]
_instance: Optional[_LEDInstance] = None


def get_led_service() -> _LEDInstance:
    """Return the singleton LED service.

    Prefers the RGB ReSpeaker service when an APA102 chain is detected on
    SPI; falls back to the ACT-LED implementation otherwise. Both expose
    ``set_pattern(name)``, ``set_transient_pattern(name | None)``,
    ``current_pattern``, and ``cleanup()``.
    """
    global _instance
    if _instance is not None:
        return _instance

    try:
        from services.respeaker_led_service import (
            RespeakerLEDService,
            respeaker_available,
        )
        if respeaker_available():
            _instance = RespeakerLEDService()
            logger.info("Using ReSpeaker APA102 RGB LEDs")
            return _instance
    except Exception as e:
        logger.warning("ReSpeaker LED init failed, falling back to ACT LED",
                       error=str(e))

    _instance = LEDService()
    return _instance
