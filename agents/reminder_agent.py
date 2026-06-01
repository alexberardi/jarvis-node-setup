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

REFRESH_INTERVAL_SECONDS = 60
IDLE_INTERVAL_SECONDS = 300


class ReminderAgent(IJarvisAgent):
    """Background agent that monitors reminders and generates time-triggered alerts."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._has_reminders: bool = False  # tracks whether any reminders exist

    @property
    def name(self) -> str:
        return "reminder_alerts"

    @property
    def description(self) -> str:
        return "Monitors reminders and generates alerts when due"

    @property
    def schedule(self) -> AgentSchedule:
        interval = REFRESH_INTERVAL_SECONDS if self._has_reminders else IDLE_INTERVAL_SECONDS
        return AgentSchedule(
            interval_seconds=interval,
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

            # Clean up expired one-shot reminders first (in-memory, no DB hit)
            service.cleanup_expired()

            # Update cached reminder count (in-memory check, no DB hit)
            all_reminders = service.get_all_reminders(include_announced=False)
            self._has_reminders = len(all_reminders) > 0

            # If no reminders exist, skip everything (no DB queries)
            if not self._has_reminders:
                self._alerts = []
                return

            settings = UserSettings("reminder")

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

                # Send push notification if enabled. Route to the reminder
                # owner's phone when we know who that is so a household
                # member's reminders don't ping everyone.
                if push_enabled:
                    self._send_push_notification(reminder.text, reminder.user_id)

                logger.info(
                    "Reminder fired",
                    reminder_id=reminder.reminder_id,
                    text=reminder.text,
                    recurrence=reminder.recurrence,
                    user_id=reminder.user_id,
                    push_sent=push_enabled,
                )

            if self._alerts:
                logger.info("Reminder agent generated alerts", count=len(self._alerts))

        except Exception as e:
            logger.error("Reminder agent run failed", error=str(e))
            self._alerts = []

    def _send_push_notification(self, text: str, user_id: int | None = None) -> None:
        """Send a push notification via command-center → jarvis-notifications.

        user_id identifies the reminder's owner; CC currently broadcasts to
        the household, but the field is passed through so per-user routing
        can be enabled server-side without another node release.
        """
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url

            cc_url = get_command_center_url()
            if not cc_url:
                logger.warning("Cannot send push notification — command center URL not configured")
                return

            payload: dict = {
                "title": "Reminder",
                "body": text,
                "priority": "high",
                "category": "reminder",
            }
            if user_id is not None:
                payload["user_id"] = user_id
                # Reminders belong to their owner — push to just their
                # phone, not every device in the household. Falls back
                # to household when we don't know the owner.
                payload["target_type"] = "user"

            result = RestClient.post(
                f"{cc_url}/api/v0/node/push-notification",
                data=payload,
                timeout=5,
            )

            if result:
                logger.debug("Push notification sent for reminder", text=text, user_id=user_id)
            else:
                logger.warning("Push notification failed for reminder", text=text, user_id=user_id)

        except Exception as e:
            logger.warning("Push notification error", error=str(e))

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)
