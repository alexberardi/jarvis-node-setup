"""Reminder service for managing voice-controlled reminders.

Handles CRUD operations, recurrence logic, snooze state, and date resolution.
Uses JarvisStorage for persistence (command_data table).
"""

import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any

from jarvis_log_client import JarvisLogger

from jarvis_command_sdk import JarvisStorage

logger = JarvisLogger(service="jarvis-node")

COMMAND_NAME = "set_reminder"
SNOOZE_WINDOW_MINUTES = 5

# Date key → day offset from today
_DATE_KEY_OFFSETS: dict[str, int] = {
    "today": 0,
    "tomorrow": 1,
    "day_after_tomorrow": 2,
    "yesterday": -1,
}

# Date key → default hour (when no explicit time given)
_DATE_KEY_DEFAULT_HOURS: dict[str, int] = {
    "morning": 7,
    "tonight": 20,
    "tomorrow_morning": 7,
    "tomorrow_afternoon": 14,
    "tomorrow_evening": 19,
    "tomorrow_night": 21,
    "yesterday_morning": 7,
    "yesterday_afternoon": 14,
    "yesterday_evening": 19,
    "last_night": 21,
}

# Keys that imply "tomorrow" + a time
_TOMORROW_TIME_KEYS: dict[str, int] = {
    "tomorrow_morning": 7,
    "tomorrow_afternoon": 14,
    "tomorrow_evening": 19,
    "tomorrow_night": 21,
}


@dataclass
class ReminderData:
    """A single reminder record."""

    reminder_id: str
    text: str
    due_at: str  # ISO 8601 with timezone
    created_at: str
    recurrence: str | None = None  # None, "daily", "weekly", "weekdays", "monthly"
    announced: bool = False
    snooze_until: str | None = None
    announce_count: int = 0
    last_announced_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReminderData":
        return cls(
            reminder_id=data["reminder_id"],
            text=data["text"],
            due_at=data["due_at"],
            created_at=data["created_at"],
            recurrence=data.get("recurrence"),
            announced=data.get("announced", False),
            snooze_until=data.get("snooze_until"),
            announce_count=data.get("announce_count", 0),
            last_announced_at=data.get("last_announced_at"),
        )

    @property
    def due_datetime(self) -> datetime:
        return datetime.fromisoformat(self.due_at)

    @property
    def is_recurring(self) -> bool:
        return self.recurrence is not None


class ReminderService:
    """Singleton service for managing reminders."""

    def __init__(self) -> None:
        self._storage = JarvisStorage(COMMAND_NAME)
        self._reminders: dict[str, ReminderData] = {}
        self._lock = threading.Lock()

    # ── CRUD ──────────────────────────────────────────────────────────

    def create_reminder(
        self,
        text: str,
        due_at: datetime,
        recurrence: str | None = None,
    ) -> ReminderData:
        """Create and persist a new reminder."""
        reminder_id = f"rem_{uuid.uuid4().hex[:8]}"
        now = datetime.now(timezone.utc)

        # Ensure due_at is timezone-aware
        if due_at.tzinfo is None:
            due_at = due_at.replace(tzinfo=timezone.utc)

        reminder = ReminderData(
            reminder_id=reminder_id,
            text=text,
            due_at=due_at.isoformat(),
            created_at=now.isoformat(),
            recurrence=recurrence,
        )

        with self._lock:
            self._reminders[reminder_id] = reminder
            self._persist(reminder)

        logger.info("Reminder created", reminder_id=reminder_id, text=text, due_at=due_at.isoformat())
        return reminder

    def get_reminder(self, reminder_id: str) -> ReminderData | None:
        with self._lock:
            return self._reminders.get(reminder_id)

    def get_all_reminders(self, include_announced: bool = False) -> list[ReminderData]:
        with self._lock:
            reminders = list(self._reminders.values())
        if not include_announced:
            reminders = [r for r in reminders if not r.announced]
        return sorted(reminders, key=lambda r: r.due_at)

    def get_due_reminders(self) -> list[ReminderData]:
        """Get reminders where due_at <= now, not announced, and not snoozed."""
        now = datetime.now(timezone.utc)
        with self._lock:
            due = []
            for r in self._reminders.values():
                if r.announced:
                    continue
                due_dt = datetime.fromisoformat(r.due_at)
                if due_dt > now:
                    continue
                if r.snooze_until:
                    snooze_dt = datetime.fromisoformat(r.snooze_until)
                    if snooze_dt > now:
                        continue
                due.append(r)
            return due

    def mark_announced(self, reminder_id: str) -> None:
        """Mark a reminder as announced. Advance recurring reminders."""
        now = datetime.now(timezone.utc)
        with self._lock:
            reminder = self._reminders.get(reminder_id)
            if not reminder:
                return

            reminder.announce_count += 1
            reminder.last_announced_at = now.isoformat()

            if reminder.is_recurring:
                # Advance to next occurrence
                next_due = self._next_occurrence(
                    datetime.fromisoformat(reminder.due_at),
                    reminder.recurrence,
                )
                reminder.due_at = next_due.isoformat()
                reminder.announced = False
                reminder.snooze_until = None
            else:
                reminder.announced = True

            self._persist(reminder)

    def snooze_reminder(self, reminder_id: str, minutes: int = 10) -> ReminderData | None:
        """Snooze a reminder for N minutes."""
        now = datetime.now(timezone.utc)
        with self._lock:
            reminder = self._reminders.get(reminder_id)
            if not reminder:
                return None

            reminder.snooze_until = (now + timedelta(minutes=minutes)).isoformat()
            reminder.announced = False
            self._persist(reminder)

        logger.info("Reminder snoozed", reminder_id=reminder_id, minutes=minutes)
        return reminder

    def delete_reminder(self, reminder_id: str) -> bool:
        with self._lock:
            if reminder_id not in self._reminders:
                return False
            del self._reminders[reminder_id]
            self._storage.delete(reminder_id)
        logger.info("Reminder deleted", reminder_id=reminder_id)
        return True

    def delete_all_reminders(self) -> int:
        with self._lock:
            count = len(self._reminders)
            self._reminders.clear()
            self._storage.delete_all()
        logger.info("All reminders deleted", count=count)
        return count

    # ── Search ────────────────────────────────────────────────────────

    def find_by_text(self, text: str) -> ReminderData | None:
        """Fuzzy match reminder by text (case-insensitive partial match)."""
        text_lower = text.lower()
        with self._lock:
            # Exact match first
            for r in self._reminders.values():
                if r.text.lower() == text_lower:
                    return r
            # Partial match
            for r in self._reminders.values():
                if text_lower in r.text.lower():
                    return r
        return None

    def find_most_recently_announced(self, window_minutes: int = SNOOZE_WINDOW_MINUTES) -> ReminderData | None:
        """Find the most recently announced reminder within the snooze window."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=window_minutes)

        with self._lock:
            candidates = []
            for r in self._reminders.values():
                if not r.last_announced_at:
                    continue
                announced_at = datetime.fromisoformat(r.last_announced_at)
                if announced_at >= cutoff:
                    candidates.append((announced_at, r))

        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # ── Startup ───────────────────────────────────────────────────────

    def restore_reminders(self) -> int:
        """Load reminders from DB into memory. Returns count restored."""
        records = self._storage.get_all()
        count = 0
        with self._lock:
            for record in records:
                try:
                    reminder = ReminderData.from_dict(record)
                    self._reminders[reminder.reminder_id] = reminder
                    count += 1
                except (KeyError, ValueError) as e:
                    logger.warning("Skipping invalid reminder record", error=str(e))
        logger.info("Reminders restored from DB", count=count)
        return count

    def cleanup_expired(self) -> int:
        """Delete one-shot reminders that are announced and past the snooze window."""
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(minutes=SNOOZE_WINDOW_MINUTES)
        to_delete: list[str] = []

        with self._lock:
            for r in self._reminders.values():
                if not r.announced or r.is_recurring:
                    continue
                if r.last_announced_at:
                    announced_at = datetime.fromisoformat(r.last_announced_at)
                    if announced_at < cutoff:
                        to_delete.append(r.reminder_id)

        for rid in to_delete:
            self.delete_reminder(rid)

        return len(to_delete)

    # ── Date Resolution ───────────────────────────────────────────────

    @staticmethod
    def resolve_due_at(
        date_keys: list[str] | None = None,
        time_str: str | None = None,
        relative_minutes: int | None = None,
    ) -> datetime | None:
        """Resolve date parameters to a concrete datetime.

        Priority:
        1. date_keys + time (if both provided)
        2. date_keys alone (uses default time from key)
        3. time alone (today or tomorrow if past)
        4. relative_minutes
        5. None (caller should error)
        """
        now = datetime.now(timezone.utc)
        local_now = datetime.now()  # naive local time for day calculations

        # Parse explicit time (HH:MM)
        hour, minute = None, None
        if time_str:
            try:
                parts = time_str.strip().split(":")
                hour = int(parts[0])
                minute = int(parts[1]) if len(parts) > 1 else 0
            except (ValueError, IndexError):
                pass

        # 1 & 2: date_keys (with optional time override)
        if date_keys:
            target_date = local_now.date()
            resolved_hour: int | None = hour

            for key in date_keys:
                key_lower = key.lower().strip()

                # Day offset keys
                if key_lower in _DATE_KEY_OFFSETS:
                    offset = _DATE_KEY_OFFSETS[key_lower]
                    target_date = (local_now + timedelta(days=offset)).date()

                # Tomorrow+time compound keys
                elif key_lower in _TOMORROW_TIME_KEYS:
                    target_date = (local_now + timedelta(days=1)).date()
                    if resolved_hour is None:
                        resolved_hour = _TOMORROW_TIME_KEYS[key_lower]

                # Weekend
                elif key_lower == "this_weekend":
                    days_until_sat = (5 - local_now.weekday()) % 7
                    if days_until_sat == 0 and local_now.weekday() != 5:
                        days_until_sat = 7
                    target_date = (local_now + timedelta(days=days_until_sat)).date()

                # Next weekday keys
                elif key_lower.startswith("next_"):
                    day_name = key_lower.replace("next_", "")
                    day_map = {
                        "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
                        "friday": 4, "saturday": 5, "sunday": 6,
                    }
                    if day_name in day_map:
                        target_weekday = day_map[day_name]
                        days_ahead = (target_weekday - local_now.weekday()) % 7
                        if days_ahead == 0:
                            days_ahead = 7
                        target_date = (local_now + timedelta(days=days_ahead)).date()

                # Time-of-day keys (no date change)
                elif key_lower in _DATE_KEY_DEFAULT_HOURS and resolved_hour is None:
                    resolved_hour = _DATE_KEY_DEFAULT_HOURS[key_lower]

            if resolved_hour is None:
                resolved_hour = 9  # Default to 9 AM if no time specified

            due = datetime(
                target_date.year, target_date.month, target_date.day,
                resolved_hour, minute or 0,
                tzinfo=timezone.utc,
            )
            return due

        # 3: time only (today or tomorrow if past)
        if hour is not None:
            due = local_now.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
            if due <= local_now:
                due += timedelta(days=1)
            return due.replace(tzinfo=timezone.utc)

        # 4: relative_minutes
        if relative_minutes is not None and relative_minutes > 0:
            return now + timedelta(minutes=relative_minutes)

        return None

    # ── Recurrence ────────────────────────────────────────────────────

    @staticmethod
    def _next_occurrence(current_due: datetime, recurrence: str | None) -> datetime:
        """Calculate the next occurrence for a recurring reminder."""
        if recurrence == "daily":
            return current_due + timedelta(days=1)
        elif recurrence == "weekly":
            return current_due + timedelta(weeks=1)
        elif recurrence == "weekdays":
            next_due = current_due + timedelta(days=1)
            while next_due.weekday() >= 5:  # Skip Sat (5) and Sun (6)
                next_due += timedelta(days=1)
            return next_due
        elif recurrence == "monthly":
            # Same day next month
            month = current_due.month + 1
            year = current_due.year
            if month > 12:
                month = 1
                year += 1
            day = min(current_due.day, 28)  # Safe for all months
            return current_due.replace(year=year, month=month, day=day)
        return current_due

    # ── Formatting ────────────────────────────────────────────────────

    @staticmethod
    def format_due_at_human(due_at_str: str) -> str:
        """Format a due_at ISO string as a human-readable string."""
        try:
            due = datetime.fromisoformat(due_at_str)
            now = datetime.now(timezone.utc)
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)

            local_due = due.astimezone()  # Convert to local
            local_now = now.astimezone()

            # Same day
            if local_due.date() == local_now.date():
                return f"today at {local_due.strftime('%-I:%M %p')}"
            # Tomorrow
            if local_due.date() == (local_now + timedelta(days=1)).date():
                return f"tomorrow at {local_due.strftime('%-I:%M %p')}"
            # This week
            days_diff = (local_due.date() - local_now.date()).days
            if 0 < days_diff <= 6:
                return f"{local_due.strftime('%A')} at {local_due.strftime('%-I:%M %p')}"
            # Further out
            return f"{local_due.strftime('%b %-d')} at {local_due.strftime('%-I:%M %p')}"
        except (ValueError, TypeError):
            return due_at_str

    # ── Internal ──────────────────────────────────────────────────────

    def _persist(self, reminder: ReminderData) -> None:
        """Save a reminder to storage."""
        self._storage.save(reminder.reminder_id, reminder.to_dict())


# ── Singleton ─────────────────────────────────────────────────────────

_service: ReminderService | None = None
_service_lock = threading.Lock()


def get_reminder_service() -> ReminderService:
    """Get or create the singleton ReminderService."""
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = ReminderService()
    return _service


def initialize_reminder_service() -> ReminderService:
    """Initialize the reminder service and restore persisted reminders."""
    service = get_reminder_service()
    service.restore_reminders()
    return service
