"""Google Calendar REST client using OAuth2 Bearer tokens.

Thin async-free wrapper around the Google Calendar v3 API.
Same interface as ICloudCalendarService: list_events via read_events().
On 401, flags re-auth so the mobile app prompts the user.
"""

from datetime import datetime, timedelta
from typing import List

import httpx

from jarvis_log_client import JarvisLogger
from jarvis_services.icloud_calendar_service import CalendarEvent

logger = JarvisLogger(service="jarvis-node")

BASE_URL = "https://www.googleapis.com/calendar/v3"


class GoogleCalendarService:
    """REST client for Google Calendar v3 API."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        calendar_id: str = "primary",
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.calendar_id = calendar_id

    def read_events(self, date: datetime | None = None, look_ahead_days: int = 1) -> List[CalendarEvent]:
        """Fetch events from Google Calendar for a date range.

        Args:
            date: Start date (default: now).
            look_ahead_days: Number of days to fetch.

        Returns:
            List of CalendarEvent objects.
        """
        if date is None:
            date = datetime.now()

        time_min = date.strftime("%Y-%m-%dT00:00:00Z")
        time_max = (date + timedelta(days=look_ahead_days)).strftime("%Y-%m-%dT00:00:00Z")

        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": "50",
        }

        try:
            response = httpx.get(
                f"{BASE_URL}/calendars/{self.calendar_id}/events",
                params=params,
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=15.0,
            )

            if response.status_code == 401:
                logger.warning("Google Calendar returned 401 — flagging re-auth")
                self._flag_reauth()
                return []

            response.raise_for_status()
            data = response.json()
            return self._parse_events(data.get("items", []))

        except httpx.HTTPStatusError as e:
            logger.error("Google Calendar API error", status_code=e.response.status_code, detail=str(e))
            return []
        except Exception as e:
            logger.error("Google Calendar request failed", error=str(e))
            return []

    def _parse_events(self, items: list[dict]) -> List[CalendarEvent]:
        """Convert Google Calendar API items to CalendarEvent objects."""
        events: list[CalendarEvent] = []
        for item in items:
            try:
                start_raw = item.get("start", {})
                end_raw = item.get("end", {})

                is_all_day = "date" in start_raw and "dateTime" not in start_raw

                if is_all_day:
                    start_time = datetime.strptime(start_raw["date"], "%Y-%m-%d")
                    end_time = datetime.strptime(end_raw.get("date", start_raw["date"]), "%Y-%m-%d")
                else:
                    start_time = self._parse_google_datetime(start_raw.get("dateTime", ""))
                    end_time = self._parse_google_datetime(end_raw.get("dateTime", ""))

                if not start_time or not end_time:
                    continue

                events.append(CalendarEvent(
                    id=item.get("id", ""),
                    summary=item.get("summary", "No Title"),
                    start_time=start_time,
                    end_time=end_time,
                    location=item.get("location"),
                    description=item.get("description"),
                    is_all_day=is_all_day,
                ))
            except Exception as e:
                logger.debug("Skipping unparseable Google Calendar event", error=str(e))
                continue
        return events

    @staticmethod
    def _parse_google_datetime(dt_str: str) -> datetime | None:
        """Parse an RFC 3339 datetime string from Google Calendar."""
        if not dt_str:
            return None
        # Google returns e.g. "2026-03-10T09:00:00-07:00"
        # Strip the colon in timezone offset for %z parsing
        if dt_str[-3] == ":" and (dt_str[-6] == "+" or dt_str[-6] == "-"):
            dt_str = dt_str[:-3] + dt_str[-2:]
        try:
            return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%S%z").replace(tzinfo=None)
        except ValueError:
            try:
                return datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                return None

    @staticmethod
    def _flag_reauth() -> None:
        """Flag the google_calendar provider as needing re-authentication."""
        try:
            from services.command_auth_service import set_needs_auth
            set_needs_auth("google_calendar", "401 Unauthorized from Google Calendar API")
        except Exception:
            pass
