"""Tests for CalendarAlertAgent."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.calendar_alert_agent import CalendarAlertAgent
from core.alert import Alert


class TestCalendarAlertAgent:
    def setup_method(self) -> None:
        self.agent = CalendarAlertAgent()

    def test_properties(self) -> None:
        assert self.agent.name == "calendar_alerts"
        assert self.agent.include_in_context is False
        assert self.agent.schedule.run_on_startup is True
        assert self.agent.schedule.interval_seconds == 300

    @patch("services.secret_service.get_secret_value")
    def test_validate_secrets_none_configured(self, mock_secret: MagicMock) -> None:
        mock_secret.return_value = None
        missing = self.agent.validate_secrets()
        assert len(missing) == 1

    @patch("services.secret_service.get_secret_value")
    def test_validate_secrets_icloud_configured(self, mock_secret: MagicMock) -> None:
        mock_secret.side_effect = lambda key, scope: "user@icloud.com" if key == "ICLOUD_USERNAME" else None
        missing = self.agent.validate_secrets()
        assert missing == []


class TestCalendarAlertProcessEvent:
    def setup_method(self) -> None:
        self.agent = CalendarAlertAgent()
        self.now = datetime.now(timezone.utc)

    def test_event_in_10_minutes_high_priority(self) -> None:
        event_start = self.now + timedelta(minutes=10)
        event = {
            "title": "Team Standup",
            "start_time": event_start.isoformat(),
        }

        self.agent._process_event(event, self.now)
        alerts = self.agent.get_alerts()

        assert len(alerts) == 1
        assert alerts[0].priority == 3
        assert "Team Standup" in alerts[0].title

    def test_event_in_45_minutes_medium_priority(self) -> None:
        event_start = self.now + timedelta(minutes=45)
        event = {
            "title": "Lunch Meeting",
            "start_time": event_start.isoformat(),
        }

        self.agent._process_event(event, self.now)
        alerts = self.agent.get_alerts()

        assert len(alerts) == 1
        assert alerts[0].priority == 2

    def test_event_in_2_hours_no_alert(self) -> None:
        event_start = self.now + timedelta(hours=2)
        event = {
            "title": "Later Meeting",
            "start_time": event_start.isoformat(),
        }

        self.agent._process_event(event, self.now)
        assert self.agent.get_alerts() == []

    def test_past_event_no_alert(self) -> None:
        event_start = self.now - timedelta(minutes=30)
        event = {
            "title": "Past Meeting",
            "start_time": event_start.isoformat(),
        }

        self.agent._process_event(event, self.now)
        assert self.agent.get_alerts() == []

    def test_dedup_same_event_same_proximity(self) -> None:
        event_start = self.now + timedelta(minutes=10)
        event = {
            "title": "Standup",
            "start_time": event_start.isoformat(),
        }

        self.agent._process_event(event, self.now)
        self.agent._process_event(event, self.now)  # duplicate
        assert len(self.agent.get_alerts()) == 1

    def test_no_start_time_no_alert(self) -> None:
        event = {"title": "All Day Event"}
        self.agent._process_event(event, self.now)
        assert self.agent.get_alerts() == []

    def test_ttl_15min_event(self) -> None:
        event_start = self.now + timedelta(minutes=10)
        event = {
            "title": "Soon",
            "start_time": event_start.isoformat(),
        }

        self.agent._process_event(event, self.now)
        alert = self.agent.get_alerts()[0]
        ttl_minutes = (alert.expires_at - alert.created_at).total_seconds() / 60
        assert ttl_minutes == pytest.approx(15, abs=0.1)

    def test_ttl_60min_event(self) -> None:
        event_start = self.now + timedelta(minutes=45)
        event = {
            "title": "Later",
            "start_time": event_start.isoformat(),
        }

        self.agent._process_event(event, self.now)
        alert = self.agent.get_alerts()[0]
        ttl_minutes = (alert.expires_at - alert.created_at).total_seconds() / 60
        assert ttl_minutes == pytest.approx(30, abs=0.1)

    def test_get_context_data_empty(self) -> None:
        assert self.agent.get_context_data() == {}
