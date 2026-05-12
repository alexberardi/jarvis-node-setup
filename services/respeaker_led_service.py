"""ReSpeakerLEDService — RGB LED control for the Seeed ReSpeaker 2-mics Pi HAT v2.

Drives 3x APA102 LEDs over SPI for command-flow visual feedback.

Patterns:
- "off"              — all LEDs off
- "normal"           — dim white (idle, default)
- "alert"            — red blink (~1Hz, used when alerts queued)
- "listening"        — blue chase (one LED at a time cycling left→middle→right)
- "thinking"         — purple slow pulse
- "speaking"         — cyan steady
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
import math
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
    return [(255, 255, 255, 8)] * NUM_LEDS


def _p_alert(t: float) -> list[tuple[int, int, int, int]]:
    if (t % 1.0) < 0.5:
        return [(255, 0, 0, 35)] * NUM_LEDS
    return [(0, 0, 0, 0)] * NUM_LEDS


def _p_listening(t: float) -> list[tuple[int, int, int, int]]:
    pos = int(t * 4) % NUM_LEDS
    out: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)] * NUM_LEDS
    out[pos] = (0, 80, 255, 40)
    return out


def _p_thinking(t: float) -> list[tuple[int, int, int, int]]:
    pulse = 0.5 + 0.5 * math.sin(t * 2 * math.pi * 0.5)
    brt = max(5, int(10 + 25 * pulse))
    return [(180, 0, 255, brt)] * NUM_LEDS


def _p_speaking(_t: float) -> list[tuple[int, int, int, int]]:
    return [(0, 200, 220, 35)] * NUM_LEDS


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
    "listening": _p_listening,
    "thinking": _p_thinking,
    "speaking": _p_speaking,
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
            if pattern == self._transient:
                return
            old = self._transient
            self._transient = pattern
        logger.debug("LED transient pattern", old=old, new=pattern)

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
            try:
                self._render(pattern, t)
            except Exception as e:
                logger.warning("LED render error", error=str(e), pattern=pattern)
            self._stop_event.wait(period)

    def _render(self, pattern: str, t: float) -> None:
        if self._strip is None:
            return
        func = _PATTERNS.get(pattern, _p_off)
        pixels = func(t)
        for i, (r, g, b, brt) in enumerate(pixels):
            self._strip.set_pixel(i, r, g, b, bright_percent=brt)
        self._strip.show()
