"""Tests for EmailAlertAgent — TDD tests written before implementation."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from agents.email_alert_agent import (
    ALERT_TTL_HOURS,
    DEFAULT_URGENT_KEYWORDS,
    MAX_ALERTS_PER_RUN,
    REFRESH_INTERVAL_SECONDS,
    EmailAlertAgent,
)
from core.alert import Alert
from jarvis_services.email_message import extract_email


def _make_email(
    msg_id: str = "msg001",
    sender: str = "Alice <alice@example.com>",
    subject: str = "Hello",
    snippet: str = "Just checking in",
    is_unread: bool = True,
) -> MagicMock:
    """Create a mock EmailMessage."""
    email = MagicMock()
    email.id = msg_id
    email.sender = sender
    email.sender_name = sender.split("<")[0].strip() if "<" in sender else sender
    email.subject = subject
    email.snippet = snippet
    email.is_unread = is_unread
    email.date = datetime.now(timezone.utc)
    return email


class TestProperties:
    def setup_method(self) -> None:
        self.agent = EmailAlertAgent()

    def test_name(self) -> None:
        assert self.agent.name == "email_alerts"

    def test_schedule_interval(self) -> None:
        assert self.agent.schedule.interval_seconds == REFRESH_INTERVAL_SECONDS
        assert self.agent.schedule.interval_seconds == 300

    def test_include_in_context_false(self) -> None:
        assert self.agent.include_in_context is False

    def test_run_on_startup_false(self) -> None:
        assert self.agent.schedule.run_on_startup is False


class TestValidation:
    def setup_method(self) -> None:
        self.agent = EmailAlertAgent()

    @patch("agents.email_alert_agent.get_secret_value")
    def test_missing_token_fails(self, mock_secret: MagicMock) -> None:
        mock_secret.return_value = None
        missing = self.agent.validate_secrets()
        assert len(missing) > 0

    @patch("agents.email_alert_agent.get_secret_value")
    def test_with_token_passes(self, mock_secret: MagicMock) -> None:
        mock_secret.return_value = "some-access-token"
        missing = self.agent.validate_secrets()
        assert missing == []


class TestVIP:
    def setup_method(self) -> None:
        self.agent = EmailAlertAgent()

    def test_vip_generates_alert(self) -> None:
        email = _make_email(sender="Boss <boss@company.com>")
        vip_senders = {"boss@company.com"}

        alerts = self.agent._check_vip(email, vip_senders)

        assert len(alerts) == 1
        assert alerts[0].priority == 3
        assert "Boss" in alerts[0].title

    def test_non_vip_no_alert(self) -> None:
        email = _make_email(sender="Random <random@example.com>")
        vip_senders = {"boss@company.com"}

        alerts = self.agent._check_vip(email, vip_senders)
        assert alerts == []

    def test_case_insensitive(self) -> None:
        email = _make_email(sender="Boss <BOSS@Company.COM>")
        vip_senders = {"boss@company.com"}

        alerts = self.agent._check_vip(email, vip_senders)
        assert len(alerts) == 1

    def test_dedup_same_id(self) -> None:
        email = _make_email(msg_id="msg001", sender="Boss <boss@company.com>")
        vip_senders = {"boss@company.com"}

        self.agent._alerted_email_ids.add("msg001")
        alerts = self.agent._check_vip(email, vip_senders)
        assert alerts == []

    def test_empty_list_disabled(self) -> None:
        email = _make_email(sender="Anyone <anyone@example.com>")
        alerts = self.agent._check_vip(email, set())
        assert alerts == []


class TestUrgent:
    def setup_method(self) -> None:
        self.agent = EmailAlertAgent()

    def test_keyword_in_subject(self) -> None:
        email = _make_email(subject="URGENT: Server is down")
        alerts = self.agent._check_urgent(email, DEFAULT_URGENT_KEYWORDS)
        assert len(alerts) == 1
        assert alerts[0].priority == 2

    def test_keyword_in_snippet(self) -> None:
        email = _make_email(subject="FYI", snippet="This needs your immediate attention ASAP")
        alerts = self.agent._check_urgent(email, DEFAULT_URGENT_KEYWORDS)
        assert len(alerts) == 1

    def test_no_match_no_alert(self) -> None:
        email = _make_email(subject="Weekly newsletter", snippet="Here are the top stories")
        alerts = self.agent._check_urgent(email, DEFAULT_URGENT_KEYWORDS)
        assert alerts == []

    def test_custom_keywords(self) -> None:
        email = _make_email(subject="Invoice overdue")
        custom = {"invoice", "overdue"}
        alerts = self.agent._check_urgent(email, custom)
        assert len(alerts) == 1

    def test_default_keywords_exist(self) -> None:
        assert "urgent" in DEFAULT_URGENT_KEYWORDS
        assert "asap" in DEFAULT_URGENT_KEYWORDS
        assert "emergency" in DEFAULT_URGENT_KEYWORDS
        assert "action required" in DEFAULT_URGENT_KEYWORDS
        assert "immediate" in DEFAULT_URGENT_KEYWORDS
        assert "critical" in DEFAULT_URGENT_KEYWORDS
        assert "deadline" in DEFAULT_URGENT_KEYWORDS

    def test_skip_already_vip_alerted(self) -> None:
        email = _make_email(msg_id="msg001", subject="URGENT from boss")
        self.agent._alerted_email_ids.add("msg001")
        alerts = self.agent._check_urgent(email, DEFAULT_URGENT_KEYWORDS)
        assert alerts == []


class TestDigest:
    def setup_method(self) -> None:
        self.agent = EmailAlertAgent()

    def test_in_morning_window(self) -> None:
        emails = [
            _make_email(msg_id=f"msg{i}", sender=f"Person{i} <p{i}@x.com>")
            for i in range(5)
        ]
        now = datetime(2026, 3, 15, 7, 30, tzinfo=timezone.utc)

        alerts = self.agent._check_digest(emails, digest_hour=7, now=now)
        assert len(alerts) == 1
        assert alerts[0].priority == 1
        assert "5 unread" in alerts[0].summary

    def test_outside_window_no_alert(self) -> None:
        emails = [_make_email()]
        now = datetime(2026, 3, 15, 14, 0, tzinfo=timezone.utc)

        alerts = self.agent._check_digest(emails, digest_hour=7, now=now)
        assert alerts == []

    def test_once_per_day(self) -> None:
        emails = [_make_email()]
        now = datetime(2026, 3, 15, 7, 30, tzinfo=timezone.utc)

        # First call succeeds
        alerts1 = self.agent._check_digest(emails, digest_hour=7, now=now)
        assert len(alerts1) == 1

        # Second call same day — no alert
        alerts2 = self.agent._check_digest(emails, digest_hour=7, now=now)
        assert alerts2 == []

    def test_no_unread_no_alert(self) -> None:
        now = datetime(2026, 3, 15, 7, 30, tzinfo=timezone.utc)
        alerts = self.agent._check_digest([], digest_hour=7, now=now)
        assert alerts == []

    def test_shows_top_senders(self) -> None:
        emails = [
            _make_email(msg_id="1", sender="Alice <alice@x.com>"),
            _make_email(msg_id="2", sender="Alice <alice@x.com>"),
            _make_email(msg_id="3", sender="Bob <bob@x.com>"),
            _make_email(msg_id="4", sender="Charlie <charlie@x.com>"),
            _make_email(msg_id="5", sender="Charlie <charlie@x.com>"),
            _make_email(msg_id="6", sender="Charlie <charlie@x.com>"),
        ]
        now = datetime(2026, 3, 15, 7, 30, tzinfo=timezone.utc)

        alerts = self.agent._check_digest(emails, digest_hour=7, now=now)
        assert len(alerts) == 1
        # Top senders should be mentioned
        assert "Charlie" in alerts[0].summary
        assert "Alice" in alerts[0].summary


class TestRateLimiting:
    def test_max_5_per_run(self) -> None:
        agent = EmailAlertAgent()
        now = datetime.now(timezone.utc)
        ttl = timedelta(hours=ALERT_TTL_HOURS)

        alerts = [
            Alert(
                source_agent="email_alerts",
                title=f"Alert {i}",
                summary=f"Alert {i}",
                created_at=now,
                expires_at=now + ttl,
                priority=3,
            )
            for i in range(10)
        ]

        trimmed = agent._apply_rate_limit(alerts)
        assert len(trimmed) == MAX_ALERTS_PER_RUN
        assert len(trimmed) == 5


class TestRun:
    def setup_method(self) -> None:
        self.agent = EmailAlertAgent()

    @pytest.mark.asyncio
    @patch("agents.email_alert_agent.get_secret_value")
    async def test_no_token_graceful(self, mock_secret: MagicMock) -> None:
        mock_secret.return_value = None
        await self.agent.run()
        assert self.agent.get_alerts() == []

    @pytest.mark.asyncio
    @patch("agents.email_alert_agent.create_email_service")
    @patch("agents.email_alert_agent.get_secret_value")
    async def test_fetches_recent_unread(
        self, mock_secret: MagicMock, mock_create: MagicMock
    ) -> None:
        mock_secret.side_effect = lambda key, scope: {
            "GMAIL_ACCESS_TOKEN": "token",
            "GMAIL_REFRESH_TOKEN": "refresh",
            "GMAIL_CLIENT_ID": "client-id",
        }.get(key)

        mock_service = MagicMock()
        mock_service.search.return_value = [
            _make_email(msg_id="msg1", sender="VIP <vip@x.com>"),
        ]
        mock_create.return_value = mock_service

        # Set VIP list
        self.agent._vip_senders = {"vip@x.com"}

        await self.agent.run()

        mock_service.search.assert_called_once()
        assert "is:unread" in mock_service.search.call_args[0][0]
        assert len(self.agent.get_alerts()) >= 1

    @pytest.mark.asyncio
    @patch("agents.email_alert_agent.create_email_service")
    @patch("agents.email_alert_agent.get_secret_value")
    async def test_email_error_graceful(
        self, mock_secret: MagicMock, mock_create: MagicMock
    ) -> None:
        mock_secret.side_effect = lambda key, scope: {
            "GMAIL_ACCESS_TOKEN": "token",
            "GMAIL_REFRESH_TOKEN": "refresh",
            "GMAIL_CLIENT_ID": "client-id",
        }.get(key)

        mock_service = MagicMock()
        mock_service.search.side_effect = RuntimeError("401 Unauthorized")
        mock_create.return_value = mock_service

        # Should not raise
        await self.agent.run()
        assert self.agent.get_alerts() == []

    @pytest.mark.asyncio
    @patch("agents.email_alert_agent.create_email_service")
    @patch("agents.email_alert_agent.get_secret_value")
    async def test_dedup_cache_trimmed(
        self, mock_secret: MagicMock, mock_create: MagicMock
    ) -> None:
        mock_secret.side_effect = lambda key, scope: {
            "GMAIL_ACCESS_TOKEN": "token",
            "GMAIL_REFRESH_TOKEN": "refresh",
            "GMAIL_CLIENT_ID": "client-id",
        }.get(key)

        mock_service = MagicMock()
        mock_service.search.return_value = []
        mock_create.return_value = mock_service

        # Pre-fill dedup cache beyond threshold
        self.agent._alerted_email_ids = {f"msg{i}" for i in range(250)}

        await self.agent.run()

        assert len(self.agent._alerted_email_ids) <= 200
