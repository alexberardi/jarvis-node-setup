"""AlertQueueService — in-memory queue for time-sensitive alerts.

Thread-safe: the scheduler thread adds alerts, the voice thread flushes them.
"""

import threading
from typing import Callable, List, Optional

from jarvis_log_client import JarvisLogger

from core.alert import Alert

logger = JarvisLogger(service="jarvis-node")

MAX_ALERTS = 50


class AlertQueueService:
    """In-memory alert queue with TTL, dedup, and change callback."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._lock = threading.Lock()
        self.on_change: Optional[Callable[[int], None]] = None

    def add_alert(self, alert: Alert) -> None:
        """Add an alert, dedup by title (case-insensitive), cap at MAX_ALERTS."""
        with self._lock:
            title_lower = alert.title.strip().lower()
            for existing in self._alerts:
                if existing.title.strip().lower() == title_lower:
                    return  # duplicate

            self._alerts.append(alert)

            # Drop oldest (lowest priority first, then oldest) if over cap
            if len(self._alerts) > MAX_ALERTS:
                self._alerts.sort(key=lambda a: (a.priority, -a.created_at.timestamp()))
                self._alerts = self._alerts[-MAX_ALERTS:]

            count = self._pending_count_unlocked()

        if self.on_change:
            try:
                self.on_change(count)
            except Exception as e:
                logger.warning("on_change callback failed", error=str(e))

    def get_pending(self) -> List[Alert]:
        """Return non-expired alerts sorted by priority desc, then created_at."""
        with self._lock:
            return self._filter_pending()

    def flush(self) -> List[Alert]:
        """Return pending alerts and clear the queue."""
        with self._lock:
            pending = self._filter_pending()
            self._alerts.clear()
            had_alerts = len(pending) > 0

        if had_alerts and self.on_change:
            try:
                self.on_change(0)
            except Exception as e:
                logger.warning("on_change callback failed", error=str(e))

        return pending

    def count(self) -> int:
        """Count non-expired alerts."""
        with self._lock:
            return self._pending_count_unlocked()

    def _filter_pending(self) -> List[Alert]:
        """Filter expired, sort by priority desc then created_at. Caller holds lock."""
        pending = [a for a in self._alerts if not a.is_expired]
        pending.sort(key=lambda a: (-a.priority, a.created_at))
        return pending

    def _pending_count_unlocked(self) -> int:
        """Count non-expired. Caller holds lock."""
        return sum(1 for a in self._alerts if not a.is_expired)

    def announce_pending_and_flush(self, led_service: Optional[object] = None) -> int:
        """Speak every pending alert via TTS, flush the queue, return the count.

        Designed for synchronous triggers like the ReSpeaker user button —
        skips the snooze/dismiss inline-listen step that the voice loop's
        ``_drain_alert_announcements`` performs, because the button press
        itself is the user's acknowledgement.

        Speaks "No new notifications." when the queue is empty so the user
        still gets feedback that the button was registered. Returns the
        number of alerts spoken (0 when nothing was pending).
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

        if led_service is not None:
            try:
                led_service.set_transient_pattern("speaking")  # type: ignore[attr-defined]
            except Exception:
                pass

        try:
            tts = get_tts_provider()
            if not pending:
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
            self.flush()
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
