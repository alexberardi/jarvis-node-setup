"""AlertQueueService — in-memory queue for time-sensitive alerts.

Thread-safe: the scheduler thread adds alerts, the voice thread flushes them.

Expiry invariant: after any public method returns, the queue contains only
non-expired alerts. Reads (count/get_pending) and writes (add_alert/flush/
remove_ids) all sweep expired entries. ``sweep_expired`` exists for periodic
callers (agent scheduler) so the queue stays clean without a user
interaction.

LED contract (``on_change``): after every public operation the callback is
invoked with the current count of *announceable* alerts — non-expired AND
``priority >= ALERT_ANNOUNCE_PRIORITY``. It is a level signal, not an edge
signal: the same value may be delivered repeatedly (the LED's set_pattern
dedups) and the periodic ``sweep_expired`` re-delivers it every scheduler
tick, so any LED/queue divergence (dropped callback, thread-ordering race
between an add and a flush) self-heals within one tick. The one asymmetry:
``flush()`` always reports 0, even when the queue was already empty, so an
explicit user dismissal can re-sync a stale LED.

Low-priority alerts (news at 1, calendar proximity at 2) stay retrievable
via the button / "what's up" but do NOT light the LED. Lighting it for
alerts the node will never proactively speak is how the LED earned a
reputation for lying — it sat purple for hours over silent priority-1
headlines the user had no idea existed.
"""

import threading
from typing import Callable, Iterable, List, Optional

from jarvis_log_client import JarvisLogger

from core.alert import Alert

logger = JarvisLogger(service="jarvis-node")

MAX_ALERTS = 50

# Single source of truth for "worth interrupting the user about". The
# announcer (core/alert_announcer.py) speaks alerts at/above this, and the
# LED lights only for them — keeping the two surfaces in lockstep so the
# LED is purple if and only if the node has something it would say.
ALERT_ANNOUNCE_PRIORITY = 3


class AlertQueueService:
    """In-memory alert queue with TTL, dedup, and change callback."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._lock = threading.Lock()
        self.on_change: Optional[Callable[[int], None]] = None

    def add_alert(self, alert: Alert) -> None:
        """Add an alert, dedup by title (case-insensitive), cap at MAX_ALERTS.

        Already-expired alerts are rejected outright — a producer handing
        over stale objects must not blip the LED for alerts nobody can
        ever retrieve.
        """
        if alert.is_expired:
            logger.warning(
                "Rejected already-expired alert",
                source_agent=alert.source_agent,
                title=alert.title,
            )
            return

        with self._lock:
            # Drop expired first so a stale same-titled entry doesn't block
            # a fresh alert via dedup.
            self._sweep_expired_unlocked()

            title_lower = alert.title.strip().lower()
            for i, existing in enumerate(self._alerts):
                if existing.title.strip().lower() == title_lower:
                    if alert.priority > existing.priority:
                        # A higher-priority re-emission must not be masked
                        # by its lower-priority twin — it may need to light
                        # the LED / get announced.
                        self._alerts[i] = alert
                        break
                    return  # duplicate
            else:
                self._alerts.append(alert)

            # Drop oldest (lowest priority first, then oldest) if over cap
            if len(self._alerts) > MAX_ALERTS:
                self._alerts.sort(key=lambda a: (a.priority, -a.created_at.timestamp()))
                self._alerts = self._alerts[-MAX_ALERTS:]

            announceable = self._announceable_count_unlocked()

        self._fire_on_change(announceable)

    def get_pending(self) -> List[Alert]:
        """Return non-expired alerts sorted by priority desc, then created_at."""
        with self._lock:
            self._sweep_expired_unlocked()
            pending = list(self._alerts)
            pending.sort(key=lambda a: (-a.priority, a.created_at))
            announceable = self._announceable_count_unlocked()

        self._fire_on_change(announceable)
        return pending

    def flush(self) -> List[Alert]:
        """Return pending alerts and clear the queue.

        Always fires ``on_change(0)`` — even when the queue was already
        empty — so an explicit user dismissal (button press, "clear my
        alerts") can re-sync a diverged LED instead of leaving it purple
        with nothing behind it.
        """
        with self._lock:
            pending = [a for a in self._alerts if not a.is_expired]
            pending.sort(key=lambda a: (-a.priority, a.created_at))
            self._alerts.clear()

        self._fire_on_change(0)
        return pending

    def remove_ids(self, alert_ids: Iterable[str]) -> int:
        """Remove specific alerts by id. Returns how many were dropped.

        Lets the announcer consume exactly what it spoke while leaving
        unspoken low-priority alerts queued for "what's up" / the button.
        """
        ids = set(alert_ids)
        with self._lock:
            before = len(self._alerts)
            self._alerts = [a for a in self._alerts if a.id not in ids]
            removed = before - len(self._alerts)
            self._sweep_expired_unlocked()
            announceable = self._announceable_count_unlocked()

        self._fire_on_change(announceable)
        return removed

    def count(self) -> int:
        """Count non-expired alerts, evicting expired entries as a side effect."""
        with self._lock:
            self._sweep_expired_unlocked()
            total = len(self._alerts)
            announceable = self._announceable_count_unlocked()

        self._fire_on_change(announceable)
        return total

    def sweep_expired(self) -> int:
        """Drop expired alerts. Returns the number evicted.

        Called every scheduler tick (~10s). Always re-delivers the current
        announceable count, so the LED reconciles to queue truth within one
        tick even after a dropped or out-of-order callback.
        """
        with self._lock:
            pre, post = self._sweep_expired_unlocked()
            announceable = self._announceable_count_unlocked()

        self._fire_on_change(announceable)
        return pre - post

    def _sweep_expired_unlocked(self) -> tuple[int, int]:
        """Evict expired alerts. Returns (pre_count, post_count). Caller holds lock."""
        pre = len(self._alerts)
        self._alerts = [a for a in self._alerts if not a.is_expired]
        return pre, len(self._alerts)

    def _announceable_count_unlocked(self) -> int:
        """Count alerts the node would proactively speak. Caller holds lock,
        after a sweep (so everything present is non-expired)."""
        return sum(1 for a in self._alerts if a.priority >= ALERT_ANNOUNCE_PRIORITY)

    def _fire_on_change(self, count: int) -> None:
        """Invoke the on_change callback safely. Must be called outside the lock."""
        if self.on_change is None:
            return
        try:
            self.on_change(count)
        except Exception as e:
            logger.warning("on_change callback failed", error=str(e))

    def announce_pending_and_flush(self, led_service: Optional[object] = None) -> int:
        """Speak every pending alert via TTS, flush the queue, return the count.

        Designed for synchronous triggers like the ReSpeaker user button —
        skips the snooze/dismiss inline-listen step that the voice loop's
        ``_drain_alert_announcements`` performs, because the button press
        itself is the user's acknowledgement.

        Speaks "No new notifications." when the queue is empty — after an
        unconditional flush, so the press re-syncs a stale LED. When the
        TTS provider is unavailable the alerts stay queued (nothing was
        heard) and a brief error pattern is shown so the press visibly
        registered. Returns the number of alerts spoken (0 when nothing
        was pending).
        """
        # Imports kept local — alert_queue_service is loaded early in main.py
        # before TTS provider config is necessarily set, and we don't want a
        # missing TTS provider to break alert *queueing*.
        try:
            from core.helpers import get_tts_provider
        except Exception as e:
            logger.warning("announce_pending_and_flush: TTS unavailable", error=str(e))
            return 0

        pending = self.get_pending()

        try:
            tts = get_tts_provider()
        except Exception as e:
            logger.error(
                "announce_pending_and_flush: TTS provider unavailable, keeping alerts",
                error=str(e),
            )
            if led_service is not None and hasattr(led_service, "preview_pattern"):
                try:
                    led_service.preview_pattern("error", 2.0)  # type: ignore[attr-defined]
                except Exception:
                    pass
            return 0

        if led_service is not None:
            try:
                led_service.set_transient_pattern("speaking")  # type: ignore[attr-defined]
            except Exception:
                pass

        try:
            if not pending:
                # Re-sync the LED without flush(): on an empty queue the
                # sweep still fires on_change(0), but an alert that landed
                # between get_pending() and here survives (flush() would
                # silently destroy it while we say "no notifications").
                self.sweep_expired()
                try:
                    tts.speak(True, "No new notifications.")
                except Exception as e:
                    logger.warning("announce empty TTS failed", error=str(e))
                return 0

            for alert in pending:
                try:
                    tts.speak(True, alert.summary)
                except Exception as e:
                    logger.warning("alert TTS failed", error=str(e),
                                   title=alert.title)
            # Remove exactly what was spoken. TTS playback takes seconds —
            # flush() here would also destroy any alert the scheduler
            # added mid-announcement, unspoken and unretrievable.
            self.remove_ids([a.id for a in pending])
            return len(pending)
        finally:
            if led_service is not None:
                try:
                    led_service.set_transient_pattern(None)  # type: ignore[attr-defined]
                except Exception:
                    pass


# Singleton
_instance: Optional[AlertQueueService] = None


def get_alert_queue_service() -> AlertQueueService:
    global _instance
    if _instance is None:
        _instance = AlertQueueService()
    return _instance
