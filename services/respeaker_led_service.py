"""ReSpeakerLEDService — RGB LED control for the Seeed ReSpeaker 2-mics Pi HAT v2.

Drives 3x APA102 LEDs over SPI for command-flow visual feedback.

Patterns:
- "off"              — all LEDs off
- "normal"           — dim white (idle, default)
- "wake_detected"    — purple steady (wake word fired, before recording starts)
- "listening"        — green steady (recording user's voice)
- "thinking"         — amber pinwheel: 1 LED lit, then 2, then 3, then loops
- "speaking"         — cyan steady (TTS response playing)
- "error"            — red steady (command failed; auto-clears with TTS)
- "not_for_me"       — orange steady (false wake detected; brief preview, auto-clears)
- "alert"            — single steady purple LED (middle, used when alerts queued)
- "muted"            — solid red (reserved for future)
- "shutdown_warning" — fast red blink (power button held)

Two-layer state: ``set_pattern()`` sets the stable pattern (idle/alert);
``set_transient_pattern(name)`` overlays a command-flow state. Passing
``None`` clears the overlay so the stable pattern resumes. This keeps the
existing alert-queue wiring (which only knows about stable patterns)
intact while letting wake/STT/TTS code lay command-flow visuals on top.

Renders at 20Hz from a single background thread. On non-Pi platforms or
when the APA102 library isn't importable, ``respeaker_available()``
returns False and the caller falls back to the ACT-LED service.
"""

from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path
from typing import Callable, Optional

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

NUM_LEDS = 3
SPIDEV_PATH = "/dev/spidev0.0"
RENDER_FPS = 20


def _try_import_apa102():
    try:
        from apa102_pi.driver.apa102 import APA102  # type: ignore
        return APA102
    except ImportError:
        try:
            from apa102 import APA102  # type: ignore
            return APA102
        except ImportError:
            return None


def respeaker_available() -> bool:
    return Path(SPIDEV_PATH).exists() and _try_import_apa102() is not None


def _p_off(_t: float) -> list[tuple[int, int, int, int]]:
    return [(0, 0, 0, 0)] * NUM_LEDS


def _p_normal(_t: float) -> list[tuple[int, int, int, int]]:
    return [(255, 255, 255, 15)] * NUM_LEDS


def _p_alert(_t: float) -> list[tuple[int, int, int, int]]:
    # Single steady purple LED in the middle position. Calm by design —
    # alerts can sit in the queue for hours and a flashing red is too
    # demanding for that duration. Same purple as wake_detected;
    # wake_detected lights all three so the patterns are visually
    # distinct even though they share a hue.
    return [(0, 0, 0, 0), (180, 0, 255, 45), (0, 0, 0, 0)]


def _p_wake_detected(_t: float) -> list[tuple[int, int, int, int]]:
    return [(180, 0, 255, 45)] * NUM_LEDS


def _p_listening(_t: float) -> list[tuple[int, int, int, int]]:
    # Green steady — distinct from the cyan "speaking" (0,200,220) and the
    # purple "wake_detected" (180,0,255). 40% brightness matches the rest
    # of the command-flow palette.
    return [(0, 255, 0, 40)] * NUM_LEDS


def _p_thinking(t: float) -> list[tuple[int, int, int, int]]:
    # Pinwheel: count of lit LEDs cycles 1 → 2 → 3 → 1 → 2 → 3, ~1 cycle/sec.
    # Phase advances every 1/3s so each step is clearly readable.
    phase = int(t * 3) % NUM_LEDS  # 0, 1, 2 → 1, 2, 3 LEDs lit
    leds_lit = phase + 1
    out: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)] * NUM_LEDS
    for i in range(leds_lit):
        out[i] = (255, 140, 0, 35)
    return out


def _p_speaking(_t: float) -> list[tuple[int, int, int, int]]:
    return [(0, 200, 220, 35)] * NUM_LEDS


def _p_error(_t: float) -> list[tuple[int, int, int, int]]:
    return [(255, 0, 0, 50)] * NUM_LEDS


def _p_not_for_me(_t: float) -> list[tuple[int, int, int, int]]:
    # Saturated orange — red-shifted enough to read distinctly from the
    # amber thinking pinwheel (255, 140, 0) at a glance.
    return [(255, 90, 0, 45)] * NUM_LEDS


def _p_muted(_t: float) -> list[tuple[int, int, int, int]]:
    return [(255, 0, 0, 30)] * NUM_LEDS


def _p_shutdown_warning(t: float) -> list[tuple[int, int, int, int]]:
    if (t % 0.2) < 0.1:
        return [(255, 0, 0, 60)] * NUM_LEDS
    return [(0, 0, 0, 0)] * NUM_LEDS


_PATTERNS: dict[str, Callable[[float], list[tuple[int, int, int, int]]]] = {
    "off": _p_off,
    "normal": _p_normal,
    "alert": _p_alert,
    "wake_detected": _p_wake_detected,
    "listening": _p_listening,
    "thinking": _p_thinking,
    "speaking": _p_speaking,
    "error": _p_error,
    "not_for_me": _p_not_for_me,
    "muted": _p_muted,
    "shutdown_warning": _p_shutdown_warning,
}


class RespeakerLEDService:
    """Drives 3x APA102 LEDs on the ReSpeaker 2-mics Pi HAT v2."""

    def __init__(self) -> None:
        self._stable: str = "normal"
        self._transient: Optional[str] = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._strip = None
        self._available = respeaker_available()
        self._enabled: bool = True
        # Scales the per-pattern bright_percent values (0-100). 100 = patterns
        # render as authored; 0 = fully dimmed (effectively off).
        self._brightness_scale: int = 100
        self._transient_timer: Optional[threading.Timer] = None

        if not self._available:
            logger.debug("ReSpeaker LED service: APA102 not available, running no-op")
            return

        APA102 = _try_import_apa102()
        try:
            assert APA102 is not None
            self._strip = APA102(num_led=NUM_LEDS, global_brightness=31, order="rgb")
            self._strip.clear_strip()
            self._strip.show()
        except Exception as e:
            logger.warning("Failed to initialize APA102 LEDs, falling back to no-op",
                           error=str(e))
            self._strip = None
            self._available = False
            return

        atexit.register(self.cleanup)
        self._start_thread()

    def set_pattern(self, pattern: str) -> None:
        with self._lock:
            if pattern == self._stable:
                return
            old = self._stable
            self._stable = pattern
        logger.debug("LED stable pattern", old=old, new=pattern)

    def set_transient_pattern(self, pattern: Optional[str]) -> None:
        with self._lock:
            # Any explicit set — including a same-value one — cancels a
            # pending preview auto-clear. Cancelling AFTER the same-value
            # early return left the stale timer alive: previewing
            # "wake_detected" and then genuinely waking within the preview
            # window let the timer clear the overlay mid-flow.
            self._cancel_timer_locked()
            self._set_transient_locked(pattern)

    def preview_pattern(self, pattern: str, duration_seconds: float = 3.0) -> None:
        """Show ``pattern`` as a transient overlay, then auto-revert.

        Used by the mobile "Test LEDs" picker so users can see each pattern
        without permanently overriding the stable state. Calling again
        cancels the previous timer. Set + arm happen in one critical
        section, and the clear callback is identity-checked — cancel()
        cannot stop an already-firing Timer, so the callback verifies it
        is still the current owner before clearing.
        """
        with self._lock:
            self._cancel_timer_locked()
            self._set_transient_locked(pattern)
            t = threading.Timer(duration_seconds, lambda: self._clear_preview(t))
            t.daemon = True
            self._transient_timer = t
            t.start()

    def _clear_preview(self, timer: threading.Timer) -> None:
        with self._lock:
            if self._transient_timer is not timer:
                return  # superseded — a newer set/preview owns the overlay
            self._transient_timer = None
            self._set_transient_locked(None)

    def _set_transient_locked(self, pattern: Optional[str]) -> None:
        if pattern == self._transient:
            return
        old = self._transient
        self._transient = pattern
        logger.debug("LED transient pattern", old=old, new=pattern)

    def _cancel_timer_locked(self) -> None:
        if self._transient_timer is not None:
            self._transient_timer.cancel()
            self._transient_timer = None

    def set_enabled(self, enabled: bool) -> None:
        """Globally enable or disable LED output.

        When disabled, the render loop still runs (so re-enabling is
        instantaneous), but every pixel is forced to (0, 0, 0).
        """
        with self._lock:
            self._enabled = bool(enabled)
        logger.debug("LED enabled", value=enabled)

    def set_brightness_scale(self, percent: int) -> None:
        """Scale per-pattern ``bright_percent`` values uniformly. 0-100."""
        percent = max(0, min(100, int(percent)))
        with self._lock:
            self._brightness_scale = percent
        logger.debug("LED brightness scale", percent=percent)

    @property
    def current_pattern(self) -> str:
        with self._lock:
            return self._transient or self._stable

    def cleanup(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._strip is not None:
            try:
                self._strip.clear_strip()
                self._strip.show()
                if hasattr(self._strip, "cleanup"):
                    self._strip.cleanup()
            except Exception as e:
                logger.debug("APA102 cleanup error (non-fatal)", error=str(e))
        logger.debug("ReSpeaker LED service cleaned up")

    def _start_thread(self) -> None:
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._render_loop, daemon=True, name="RespeakerLEDRender"
        )
        self._thread.start()

    def _render_loop(self) -> None:
        period = 1.0 / RENDER_FPS
        t0 = time.monotonic()
        while not self._stop_event.is_set():
            t = time.monotonic() - t0
            with self._lock:
                pattern = self._transient or self._stable
                enabled = self._enabled
                scale = self._brightness_scale
            try:
                if enabled:
                    self._render(pattern, t, scale)
                else:
                    self._render("off", t, 0)
            except Exception as e:
                logger.warning("LED render error", error=str(e), pattern=pattern)
            self._stop_event.wait(period)

    def _render(self, pattern: str, t: float, scale: int = 100) -> None:
        if self._strip is None:
            return
        func = _PATTERNS.get(pattern, _p_off)
        pixels = func(t)
        for i, (r, g, b, brt) in enumerate(pixels):
            scaled_brt = max(0, min(100, int(brt * scale / 100)))
            self._strip.set_pixel(i, r, g, b, bright_percent=scaled_brt)
        self._strip.show()
