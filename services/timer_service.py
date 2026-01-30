"""
Timer service for managing background timers with TTS notifications.

Uses threading.Timer for non-blocking timer execution. When a timer completes,
it triggers TTS via the configured provider to announce completion.
"""

import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional


@dataclass
class TimerInfo:
    """Information about an active timer"""
    timer_id: str
    label: Optional[str]
    duration_seconds: int
    started_at: datetime
    ends_at: datetime
    _timer: threading.Timer

    def time_remaining_seconds(self) -> float:
        """Get remaining time in seconds"""
        remaining = (self.ends_at - datetime.now()).total_seconds()
        return max(0, remaining)


class TimerService:
    """
    Singleton service for managing timers.

    Timers run in background threads and trigger TTS announcements on completion.
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
        timer_id = str(uuid.uuid4())[:8]  # Short ID for easy reference

        now = datetime.now()
        ends_at = datetime.fromtimestamp(now.timestamp() + duration_seconds)

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
            _timer=timer
        )

        with self._timers_lock:
            self._timers[timer_id] = timer_info

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
            timer_info._timer.cancel()
            return True

    def get_active_timers(self) -> List[Dict[str, any]]:
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

    def get_timer(self, timer_id: str) -> Optional[Dict[str, any]]:
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

    def _on_timer_complete(self, timer_id: str) -> None:
        """Internal callback when a timer fires"""
        with self._timers_lock:
            timer_info = self._timers.pop(timer_id, None)

        if timer_info is None:
            return  # Timer was cancelled

        # Invoke the completion callback if set
        if self._on_complete_callback:
            try:
                self._on_complete_callback(timer_id, timer_info.label)
            except Exception as e:
                # Log but don't crash - timer completion shouldn't fail silently
                print(f"[TimerService] Error in completion callback: {e}")

    def clear_all(self) -> int:
        """
        Cancel all active timers.

        Returns:
            Number of timers cancelled
        """
        with self._timers_lock:
            count = len(self._timers)
            for timer_info in self._timers.values():
                timer_info._timer.cancel()
            self._timers.clear()
            return count


def get_timer_service() -> TimerService:
    """Get the singleton TimerService instance"""
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
        print(f"[TimerService] Failed to speak timer completion: {e}")


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
