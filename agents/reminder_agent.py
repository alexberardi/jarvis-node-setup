"""ReminderAgent — monitors reminders and generates alerts when due.

Runs every 30 seconds. Produces Alert objects for due reminders via the
existing alert queue pattern. One-shot reminders are marked announced;
recurring reminders advance to the next occurrence.

Inline listen / TTS announcement is a follow-up enhancement.
"""

from datetime import timedelta, timezone, datetime
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.alert import Alert
from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import IJarvisSecret

logger = JarvisLogger(service="jarvis-node")

REFRESH_INTERVAL_SECONDS = 30


class ReminderAgent(IJarvisAgent):
    """Background agent that monitors reminders and generates time-triggered alerts."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []

    @property
    def name(self) -> str:
        return "reminder_alerts"

    @property
    def description(self) -> str:
        return "Monitors reminders and generates alerts when due"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=True,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # No external services needed

    @property
    def include_in_context(self) -> bool:
        return False  # Side-effect only, no context injection

    async def run(self) -> None:
        """Check for due reminders and generate alerts."""
        try:
            from services.reminder_service import get_reminder_service

            service = get_reminder_service()

            # Clean up expired one-shot reminders
            service.cleanup_expired()

            # Check for due reminders
            due_reminders = service.get_due_reminders()
            self._alerts = []

            now = datetime.now(timezone.utc)

            for reminder in due_reminders:
                self._alerts.append(Alert(
                    source_agent=self.name,
                    title=f"Reminder: {reminder.text}",
                    summary=f"Reminder: {reminder.text}",
                    created_at=now,
                    expires_at=now + timedelta(minutes=10),
                    priority=3,
                ))

                # Mark as announced (advances recurring reminders automatically)
                service.mark_announced(reminder.reminder_id)

                logger.info(
                    "Reminder fired",
                    reminder_id=reminder.reminder_id,
                    text=reminder.text,
                    recurrence=reminder.recurrence,
                )

            if self._alerts:
                logger.info("Reminder agent generated alerts", count=len(self._alerts))

        except Exception as e:
            logger.error("Reminder agent run failed", error=str(e))
            self._alerts = []

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)
