"""CalendarAlertAgent — monitors calendar events and generates time-proximity alerts.

Runs every 5 minutes. Produces alerts based on how soon events are:
- Event in <=15 min -> priority 3, TTL 15 min
- Event in <=60 min -> priority 2, TTL 30 min

Requires calendar secrets to be configured (skipped otherwise via standard
agent discovery secret validation).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw): self._log = logging.getLogger(kw.get("service", __name__))
        def info(self, msg, **kw): self._log.info(msg)
        def warning(self, msg, **kw): self._log.warning(msg)
        def error(self, msg, **kw): self._log.error(msg)
        def debug(self, msg, **kw): self._log.debug(msg)

from jarvis_command_sdk import (
    AgentSchedule,
    Alert,
    IJarvisAgent,
    IJarvisSecret,
    JarvisSecret,
    JarvisStorage,
    RequestInformation,
)

logger = JarvisLogger(service="jarvis-node")

REFRESH_INTERVAL_SECONDS = 300  # 5 minutes

_storage = JarvisStorage("calendar_alerts")


class CalendarAlertAgent(IJarvisAgent):
    """Background agent that monitors calendar for upcoming events."""

    def __init__(self) -> None:
        self._alerts: List[Alert] = []
        self._alerted_event_keys: set[str] = set()  # track already-alerted events

    @property
    def name(self) -> str:
        return "calendar_alerts"

    @property
    def description(self) -> str:
        return "Monitors calendar events and generates time-proximity alerts"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=True,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        # At least one calendar provider must be configured
        return [
            JarvisSecret(
                "ICLOUD_USERNAME",
                "iCloud username for calendar access",
                "integration",
                "string",
                required=False,
            ),
            JarvisSecret(
                "GOOGLE_CALENDAR_CREDENTIALS",
                "Google Calendar OAuth credentials JSON",
                "integration",
                "string",
                required=False,
            ),
        ]

    def validate_secrets(self) -> List[str]:
        """Override: at least one calendar provider must be configured."""
        has_icloud = bool(_storage.get_secret("ICLOUD_USERNAME"))
        has_google = bool(_storage.get_secret("GOOGLE_CALENDAR_CREDENTIALS"))

        if not has_icloud and not has_google:
            return ["ICLOUD_USERNAME or GOOGLE_CALENDAR_CREDENTIALS"]
        return []

    @property
    def include_in_context(self) -> bool:
        return False

    async def run(self) -> None:
        """Fetch today's calendar events and generate time-proximity alerts."""
        try:
            try:
                from commands.get_calendar_events.command import ReadCalendarCommand
            except ImportError:
                from commands.custom_commands.get_calendar_events.command import ReadCalendarCommand

            cmd = ReadCalendarCommand()
            today = datetime.now().strftime("%Y-%m-%d")

            request_info = RequestInformation(
                voice_command="calendar check",
                conversation_id="calendar-alert-agent",
            )

            response = cmd.run(
                request_info,
                resolved_datetimes=[today],
            )

            if not response.success or not response.context_data:
                self._alerts = []
                return

            events = response.context_data.get("events", [])
            now = datetime.now(timezone.utc)
            self._alerts = []

            for event in events:
                self._process_event(event, now)

        except Exception as e:
            logger.error("Calendar agent run failed", error=str(e))
            self._alerts = []

    def _process_event(self, event: Dict[str, Any], now: datetime) -> None:
        """Generate an alert if an event is within the alert window."""
        start_str = event.get("start_time") or event.get("start")
        title = event.get("title") or event.get("summary", "Untitled event")

        if not start_str:
            return

        try:
            # Parse ISO format
            if isinstance(start_str, str):
                start_str = start_str.replace("Z", "+00:00")
                event_start = datetime.fromisoformat(start_str)
                if event_start.tzinfo is None:
                    event_start = event_start.replace(tzinfo=timezone.utc)
            else:
                return
        except (ValueError, TypeError):
            return

        minutes_until = (event_start - now).total_seconds() / 60

        # Only alert for future events within 60 minutes
        if minutes_until < 0 or minutes_until > 60:
            return

        # Dedup: don't re-alert for the same event at the same proximity level
        if minutes_until <= 15:
            event_key = f"{title}:15min"
            priority = 3
            ttl = timedelta(minutes=15)
            time_desc = f"in {int(minutes_until)} minutes" if minutes_until > 1 else "starting now"
        else:
            event_key = f"{title}:60min"
            priority = 2
            ttl = timedelta(minutes=30)
            time_desc = f"in about {int(minutes_until)} minutes"

        if event_key in self._alerted_event_keys:
            return

        self._alerted_event_keys.add(event_key)

        self._alerts.append(Alert(
            source_agent=self.name,
            title=f"Upcoming: {title}",
            summary=f"{title} {time_desc}",
            created_at=now,
            expires_at=now + ttl,
            priority=priority,
        ))

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)
