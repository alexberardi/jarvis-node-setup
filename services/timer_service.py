"""
Timer service for managing background timers with TTS notifications.

Uses threading.Timer for non-blocking timer execution. When a timer completes,
it triggers TTS via the configured provider to announce completion.

Supports persistence: timers survive node restarts via the command_data table.
"""

import logging
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from db import SessionLocal
from repositories.command_data_repository import CommandDataRepository


# Command name used for persistence
TIMER_COMMAND_NAME = "set_timer"

# Timer ID length (8-char UUID prefix)
TIMER_ID_LENGTH = 8

# Module logger - uses standard logging, integrates with JarvisLogger if configured
logger = logging.getLogger(__name__)


@dataclass
class TimerInfo:
    """Information about an active timer."""

    timer_id: str
    label: Optional[str]
    duration_seconds: int
    started_at: datetime
    ends_at: datetime
    timer: threading.Timer  # Public - accessed by cancel methods

    def time_remaining_seconds(self) -> float:
        """Get remaining time in seconds."""
        now = datetime.now(timezone.utc)
        ends_at = self.ends_at
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)
        remaining = (ends_at - now).total_seconds()
        return max(0, remaining)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize timer info for persistence."""
        return {
            "timer_id": self.timer_id,
            "label": self.label,
            "duration_seconds": self.duration_seconds,
            "started_at": self.started_at.isoformat(),
            "ends_at": self.ends_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], timer: threading.Timer) -> "TimerInfo":
        """Deserialize timer info from persistence."""
        started_at = datetime.fromisoformat(data["started_at"])
        ends_at = datetime.fromisoformat(data["ends_at"])

        # Ensure timezone awareness
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)
        if ends_at.tzinfo is None:
            ends_at = ends_at.replace(tzinfo=timezone.utc)

        return cls(
            timer_id=data.get("timer_id", data.get("_data_key", "")),
            label=data.get("label"),
            duration_seconds=data.get("duration_seconds", 0),
            started_at=started_at,
            ends_at=ends_at,
            timer=timer,
        )


class TimerService:
    """
    Singleton service for managing timers.

    Timers run in background threads and trigger TTS announcements on completion.
    Supports persistence via command_data table for restart survival.
    """

    _instance: Optional["TimerService"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "TimerService":
        if cls._instance is None:
            with cls._lock:
                # Double-check locking pattern
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        # Thread-safe initialization check
        with self._lock:
            if self._initialized:
                return
            self._timers: Dict[str, TimerInfo] = {}
            self._timers_lock: threading.Lock = threading.Lock()
            self._on_complete_callback: Optional[Callable[[str, Optional[str]], None]] = None
            self._initialized = True

    def set_on_complete_callback(
        self,
        callback: Callable[[str, Optional[str]], None]
    ) -> None:
        """
        Set callback to be invoked when a timer completes.

        Args:
            callback: Function taking (timer_id, label) to call on completion
        """
        self._on_complete_callback = callback

    def set_timer(
        self,
        duration_seconds: int,
        label: Optional[str] = None
    ) -> str:
        """
        Create a new timer.

        Args:
            duration_seconds: Duration in seconds until timer fires
            label: Optional label for the timer (e.g., "pasta", "laundry")

        Returns:
            Timer ID that can be used to cancel or query the timer
        """
        timer_id = str(uuid.uuid4())[:TIMER_ID_LENGTH]

        # Use timezone-aware datetimes consistently
        now = datetime.now(timezone.utc)
        ends_at = datetime.fromtimestamp(now.timestamp() + duration_seconds, tz=timezone.utc)

        # Create the timer with daemon=True so it doesn't block app shutdown
        timer = threading.Timer(
            duration_seconds,
            self._on_timer_complete,
            args=(timer_id,)
        )
        timer.daemon = True

        timer_info = TimerInfo(
            timer_id=timer_id,
            label=label,
            duration_seconds=duration_seconds,
            started_at=now,
            ends_at=ends_at,
            timer=timer
        )

        with self._timers_lock:
            self._timers[timer_id] = timer_info

        # Persist the timer for restart survival
        self._persist_timer(timer_id, timer_info)

        timer.start()
        return timer_id

    def cancel_timer(self, timer_id: str) -> bool:
        """
        Cancel an active timer.

        Args:
            timer_id: The ID of the timer to cancel

        Returns:
            True if timer was found and cancelled, False otherwise
        """
        with self._timers_lock:
            timer_info = self._timers.pop(timer_id, None)
            if timer_info is None:
                return False
            timer_info.timer.cancel()

        # Remove from persistence
        self._delete_persisted_timer(timer_id)
        return True

    def get_active_timers(self) -> List[Dict[str, Any]]:
        """
        Get information about all active timers.

        Returns:
            List of dicts with timer information (id, label, remaining_seconds)
        """
        with self._timers_lock:
            return [
                {
                    "timer_id": info.timer_id,
                    "label": info.label,
                    "duration_seconds": info.duration_seconds,
                    "remaining_seconds": int(info.time_remaining_seconds()),
                    "started_at": info.started_at.isoformat(),
                    "ends_at": info.ends_at.isoformat(),
                }
                for info in self._timers.values()
            ]

    def get_timer(self, timer_id: str) -> Optional[Dict[str, Any]]:
        """
        Get information about a specific timer.

        Args:
            timer_id: The ID of the timer to query

        Returns:
            Dict with timer info or None if not found
        """
        with self._timers_lock:
            info = self._timers.get(timer_id)
            if info is None:
                return None
            return {
                "timer_id": info.timer_id,
                "label": info.label,
                "duration_seconds": info.duration_seconds,
                "remaining_seconds": int(info.time_remaining_seconds()),
                "started_at": info.started_at.isoformat(),
                "ends_at": info.ends_at.isoformat(),
            }

    def find_timer_by_label(self, label: str) -> Optional[str]:
        """
        Find a timer by its label (case-insensitive partial match).

        Args:
            label: The label to search for

        Returns:
            Timer ID if found, None otherwise
        """
        label_lower = label.lower()
        with self._timers_lock:
            for timer_id, info in self._timers.items():
                if info.label and label_lower in info.label.lower():
                    return timer_id
        return None

    def restore_timers(self) -> int:
        """
        Restore timers from database after restart.

        - Expired timers: trigger callback immediately
        - Active timers: recreate threading.Timer with remaining time

        Returns:
            Count of active timers restored (not counting expired)
        """
        try:
            with SessionLocal() as session:
                repo = CommandDataRepository(session)
                # Get all timers including expired (we'll handle them)
                persisted_timers = repo.get_all(TIMER_COMMAND_NAME, include_expired=True)
        except Exception as e:
            logger.error("Failed to load persisted timers: %s", e)
            return 0

        restored_count = 0
        expired_timers: List[Dict[str, Any]] = []
        now = datetime.now(timezone.utc)

        for timer_data in persisted_timers:
            timer_id = timer_data.get("_data_key", timer_data.get("timer_id"))
            if not timer_id:
                continue

            try:
                ends_at = datetime.fromisoformat(timer_data["ends_at"])
                if ends_at.tzinfo is None:
                    ends_at = ends_at.replace(tzinfo=timezone.utc)

                remaining = (ends_at - now).total_seconds()

                if remaining <= 0:
                    # Timer has expired while we were down
                    expired_timers.append(timer_data)
                else:
                    # Restore active timer
                    timer = threading.Timer(
                        remaining,
                        self._on_timer_complete,
                        args=(timer_id,)
                    )
                    timer.daemon = True

                    timer_info = TimerInfo.from_dict(timer_data, timer)

                    with self._timers_lock:
                        self._timers[timer_id] = timer_info

                    timer.start()
                    restored_count += 1
                    logger.info(
                        "Restored timer '%s' with %ds remaining",
                        timer_data.get("label", timer_id),
                        int(remaining)
                    )

            except (KeyError, ValueError) as e:
                logger.warning("Failed to restore timer %s: %s", timer_id, e)
                # Clean up corrupt record
                self._delete_persisted_timer(timer_id)

        # Fire callbacks for expired timers
        for timer_data in expired_timers:
            timer_id = timer_data.get("_data_key", timer_data.get("timer_id"))
            label = timer_data.get("label")
            logger.info("Timer '%s' expired while down, firing now", label or timer_id)

            # Clean up the persisted record
            self._delete_persisted_timer(timer_id)

            # Fire the callback
            if self._on_complete_callback:
                try:
                    self._on_complete_callback(timer_id, label)
                except Exception as e:
                    logger.error("Error in completion callback for expired timer: %s", e)

        return restored_count

    def _on_timer_complete(self, timer_id: str) -> None:
        """Internal callback when a timer fires."""
        with self._timers_lock:
            timer_info = self._timers.pop(timer_id, None)

        if timer_info is None:
            return  # Timer was cancelled

        # Remove from persistence
        self._delete_persisted_timer(timer_id)

        # Invoke the completion callback if set
        if self._on_complete_callback:
            try:
                self._on_complete_callback(timer_id, timer_info.label)
            except Exception as e:
                logger.error("Error in completion callback: %s", e)

    def clear_all(self) -> int:
        """
        Cancel all active timers.

        Returns:
            Number of timers cancelled
        """
        with self._timers_lock:
            count = len(self._timers)
            for timer_info in self._timers.values():
                timer_info.timer.cancel()
            self._timers.clear()

        # Clear all persisted timers
        try:
            with SessionLocal() as session:
                repo = CommandDataRepository(session)
                repo.delete_all(TIMER_COMMAND_NAME)
        except Exception as e:
            logger.error("Failed to clear persisted timers: %s", e)

        return count

    def _persist_timer(self, timer_id: str, timer_info: TimerInfo) -> None:
        """Save timer to database for restart survival."""
        try:
            # Ensure ends_at is timezone-aware for expiration handling
            ends_at_utc = timer_info.ends_at
            if ends_at_utc.tzinfo is None:
                ends_at_utc = ends_at_utc.replace(tzinfo=timezone.utc)

            with SessionLocal() as session:
                repo = CommandDataRepository(session)
                repo.save(
                    command_name=TIMER_COMMAND_NAME,
                    data_key=timer_id,
                    data=timer_info.to_dict(),
                    expires_at=ends_at_utc,
                )
        except Exception as e:
            logger.error("Failed to persist timer %s: %s", timer_id, e)

    def _delete_persisted_timer(self, timer_id: str) -> None:
        """Remove timer from database."""
        try:
            with SessionLocal() as session:
                repo = CommandDataRepository(session)
                repo.delete(TIMER_COMMAND_NAME, timer_id)
        except Exception as e:
            logger.error("Failed to delete persisted timer %s: %s", timer_id, e)


def get_timer_service() -> TimerService:
    """Get the singleton TimerService instance."""
    return TimerService()


def _default_timer_complete_handler(timer_id: str, label: Optional[str]) -> None:
    """
    Default handler for timer completion - announces via TTS.

    Args:
        timer_id: The ID of the completed timer
        label: Optional label for the timer
    """
    # Import here to avoid circular imports and ensure TTS is configured
    from core.helpers import get_tts_provider

    try:
        tts = get_tts_provider()
        if label:
            message = f"Your {label} timer is done!"
        else:
            message = "Your timer is done!"
        tts.speak(True, message)
    except Exception as e:
        logger.error("Failed to speak timer completion: %s", e)


def initialize_timer_service() -> TimerService:
    """
    Initialize the timer service with default TTS callback.

    Call this during application startup to set up the timer service
    with the default TTS announcement handler.

    Returns:
        The initialized TimerService instance
    """
    service = get_timer_service()
    service.set_on_complete_callback(_default_timer_complete_handler)
    return service
