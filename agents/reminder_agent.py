"""ReminderAgent — monitors reminders and generates alerts when due.

Runs every 30 seconds. Produces Alert objects for due reminders via the
existing alert queue pattern. One-shot reminders are marked announced;
recurring reminders advance to the next occurrence.

When REMINDER_PUSH_NOTIFICATIONS is enabled, also sends push notifications
to the user's phone via command-center → jarvis-notifications.
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
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    async def run(self) -> None:
        """Check for due reminders and generate alerts."""
        try:
            from services.reminder_service import get_reminder_service
            from jarvis_command_sdk import UserSettings

            service = get_reminder_service()
            settings = UserSettings("reminder")

            # Clean up expired one-shot reminders
            service.cleanup_expired()

            # Check for due reminders
            due_reminders = service.get_due_reminders()
            self._alerts = []

            now = datetime.now(timezone.utc)
            push_enabled = settings.is_enabled("push_notifications")

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

                # Send push notification if enabled
                if push_enabled:
                    self._send_push_notification(reminder.text)

                logger.info(
                    "Reminder fired",
                    reminder_id=reminder.reminder_id,
                    text=reminder.text,
                    recurrence=reminder.recurrence,
                    push_sent=push_enabled,
                )

            if self._alerts:
                logger.info("Reminder agent generated alerts", count=len(self._alerts))

        except Exception as e:
            logger.error("Reminder agent run failed", error=str(e))
            self._alerts = []

    def _send_push_notification(self, text: str) -> None:
        """Send a push notification via command-center → jarvis-notifications."""
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url

            cc_url = get_command_center_url()
            if not cc_url:
                logger.warning("Cannot send push notification — command center URL not configured")
                return

            result = RestClient.post(
                f"{cc_url}/api/v0/node/push-notification",
                data={
                    "title": "Reminder",
                    "body": text,
                    "priority": "high",
                    "category": "reminder",
                },
                timeout=5,
            )

            if result:
                logger.debug("Push notification sent for reminder", text=text)
            else:
                logger.warning("Push notification failed for reminder", text=text)

        except Exception as e:
            logger.warning("Push notification error", error=str(e))

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)
