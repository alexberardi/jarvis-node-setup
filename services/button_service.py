"""ButtonService — GPIO17 button on the ReSpeaker 2-mics Pi HAT v2.

Behavior:
- Short press (<3s):    publish ``jarvis/nodes/<node_id>/button/notifications_request``
                        so the command-center can speak any queued notifications.
- Long hold (>=3s):     publish ``jarvis/nodes/<node_id>/button/shutdown`` and
                        run ``sudo systemctl poweroff`` for a clean shutdown.
- During hold (>=1s):   LED service shows the ``shutdown_warning`` transient so
                        the user sees they're heading for poweroff.

Pi Zero 2 W has no software wake-on-GPIO from a halted state — after a
button-triggered poweroff, power must be re-cycled (unplug/replug) to bring
the node back up. Documented in the node README; not a software bug.

On non-Pi platforms or when ``gpiozero`` is unavailable, the service is a no-op
and logs that fact.
"""

from __future__ import annotations

import json
import subprocess
import threading
import time
from typing import Any, Optional

from jarvis_log_client import JarvisLogger
from utils.config_service import Config

logger = JarvisLogger(service="jarvis-node")

BUTTON_GPIO = 17
HOLD_FOR_POWEROFF_SECS = 3.0
SHUTDOWN_WARNING_AT_SECS = 1.0
BOUNCE_TIME_SECS = 0.05

NOTIFICATIONS_TOPIC_TEMPLATE = "jarvis/nodes/{node_id}/button/notifications_request"
SHUTDOWN_TOPIC_TEMPLATE = "jarvis/nodes/{node_id}/button/shutdown"


def _try_import_gpiozero():
    try:
        from gpiozero import Button  # type: ignore
        return Button
    except Exception as e:
        logger.debug("gpiozero not available", error=str(e))
        return None


class ButtonService:
    """Wraps the ReSpeaker user button.

    Constructed with an LED service (used for the shutdown_warning transient)
    and a callable that returns the current MQTT client (lazy so the button
    service can be started before MQTT is connected).
    """

    def __init__(
        self,
        led_service: Any,
        mqtt_client_provider,
    ) -> None:
        self._led = led_service
        self._mqtt_client_provider = mqtt_client_provider
        self._press_time: Optional[float] = None
        self._is_held = False
        self._warning_timer: Optional[threading.Timer] = None
        self._button = None
        self._available = False

        Button = _try_import_gpiozero()
        if Button is None:
            logger.info("Button service: gpiozero unavailable, running no-op")
            return

        try:
            self._button = Button(
                BUTTON_GPIO,
                pull_up=True,
                bounce_time=BOUNCE_TIME_SECS,
                hold_time=HOLD_FOR_POWEROFF_SECS,
            )
            self._button.when_pressed = self._on_pressed
            self._button.when_released = self._on_released
            self._button.when_held = self._on_held
            self._available = True
            logger.info("Button service started", gpio=BUTTON_GPIO,
                        hold_secs=HOLD_FOR_POWEROFF_SECS)
        except Exception as e:
            logger.warning("Failed to attach button on GPIO17 (running no-op)",
                           error=str(e))
            self._button = None

    @property
    def available(self) -> bool:
        return self._available

    def cleanup(self) -> None:
        if self._warning_timer is not None:
            self._warning_timer.cancel()
            self._warning_timer = None
        if self._button is not None:
            try:
                self._button.close()
            except Exception as e:
                logger.debug("Button close error (non-fatal)", error=str(e))

    def _on_pressed(self) -> None:
        self._press_time = time.monotonic()
        self._is_held = False
        self._warning_timer = threading.Timer(
            SHUTDOWN_WARNING_AT_SECS, self._show_warning
        )
        self._warning_timer.daemon = True
        self._warning_timer.start()
        logger.debug("Button pressed")

    def _show_warning(self) -> None:
        try:
            self._led.set_transient_pattern("shutdown_warning")
        except Exception as e:
            logger.warning("LED transient set failed", error=str(e))

    def _on_released(self) -> None:
        held_duration = (
            time.monotonic() - self._press_time if self._press_time else 0.0
        )
        if self._warning_timer is not None:
            self._warning_timer.cancel()
            self._warning_timer = None
        try:
            self._led.set_transient_pattern(None)
        except Exception:
            pass

        logger.debug("Button released", held_secs=round(held_duration, 2),
                     was_held=self._is_held)

        if self._is_held:
            return  # Shutdown already initiated by _on_held
        self._publish_notifications_request()
        self._press_time = None

    def _on_held(self) -> None:
        self._is_held = True
        try:
            self._led.set_transient_pattern("shutdown_warning")
        except Exception:
            pass

        logger.info("Button held >= %.1fs — initiating shutdown",
                    HOLD_FOR_POWEROFF_SECS)
        self._publish_shutdown()
        # Give the broker ~250ms to flush the event before powering off
        time.sleep(0.25)
        self._do_poweroff()

    def _do_poweroff(self) -> None:
        try:
            subprocess.Popen(
                ["sudo", "-n", "systemctl", "poweroff"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            logger.error("systemctl poweroff failed", error=str(e))

    def _publish_notifications_request(self) -> None:
        client = self._mqtt_client_provider()
        if client is None:
            logger.warning("Button: notifications_request — MQTT client not ready")
            return
        node_id = Config.get_str("node_id", "") or ""
        if not node_id:
            logger.warning("Button: node_id not configured, skipping publish")
            return
        topic = NOTIFICATIONS_TOPIC_TEMPLATE.format(node_id=node_id)
        payload = json.dumps({"node_id": node_id, "ts": time.time()})
        try:
            client.publish(topic, payload, qos=1)
            logger.info("Published notifications_request", topic=topic)
        except Exception as e:
            logger.error("Failed to publish notifications_request",
                         topic=topic, error=str(e))

    def _publish_shutdown(self) -> None:
        client = self._mqtt_client_provider()
        if client is None:
            return
        node_id = Config.get_str("node_id", "") or ""
        if not node_id:
            return
        topic = SHUTDOWN_TOPIC_TEMPLATE.format(node_id=node_id)
        payload = json.dumps({"node_id": node_id, "reason": "button_long_press",
                              "ts": time.time()})
        try:
            client.publish(topic, payload, qos=1)
            logger.info("Published shutdown notification", topic=topic)
        except Exception as e:
            logger.error("Failed to publish shutdown event",
                         topic=topic, error=str(e))


_instance: Optional[ButtonService] = None


def get_button_service(
    led_service: Any,
    mqtt_client_provider,
) -> ButtonService:
    """Singleton accessor. First call wires LED + MQTT providers."""
    global _instance
    if _instance is None:
        _instance = ButtonService(led_service, mqtt_client_provider)
    return _instance
