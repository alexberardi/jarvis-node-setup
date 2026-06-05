"""Tests for WhatsUpCommand."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from commands.whats_up_command import WhatsUpCommand
from core.alert import Alert
from core.request_information import RequestInformation
from services.alert_queue_service import AlertQueueService


def _make_alert(title: str = "Test alert", priority: int = 2) -> Alert:
    now = datetime.now(timezone.utc)
    return Alert(
        source_agent="test",
        title=title,
        summary=f"Summary for {title}",
        created_at=now,
        expires_at=now + timedelta(hours=1),
        priority=priority,
    )


def _make_request_info() -> RequestInformation:
    return RequestInformation(
        voice_command="what's up",
        conversation_id="test-conv-1",
    )


class TestWhatsUpPreRoute:
    def setup_method(self) -> None:
        self.cmd = WhatsUpCommand()
        self.queue = AlertQueueService()

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_pre_route_matches_whats_up(self, mock_get_queue: MagicMock) -> None:
        self.queue.add_alert(_make_alert("News flash"))
        mock_get_queue.return_value = self.queue

        result = self.cmd.pre_route("What's up?")
        assert result is not None
        alerts = json.loads(result.arguments["alerts_json"])
        assert len(alerts) == 1
        assert alerts[0]["title"] == "News flash"

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_pre_route_matches_any_alerts(self, mock_get_queue: MagicMock) -> None:
        self.queue.add_alert(_make_alert("Calendar event"))
        mock_get_queue.return_value = self.queue

        result = self.cmd.pre_route("any alerts")
        assert result is not None

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_pre_route_no_alerts_returns_none(self, mock_get_queue: MagicMock) -> None:
        mock_get_queue.return_value = self.queue  # empty queue

        result = self.cmd.pre_route("what's up")
        assert result is None

    def test_pre_route_non_matching_phrase_returns_none(self) -> None:
        result = self.cmd.pre_route("turn off the lights")
        assert result is None

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_pre_route_flushes_queue(self, mock_get_queue: MagicMock) -> None:
        self.queue.add_alert(_make_alert("Alert 1"))
        self.queue.add_alert(_make_alert("Alert 2"))
        mock_get_queue.return_value = self.queue

        self.cmd.pre_route("whats up")
        assert self.queue.count() == 0


class TestWhatsUpRun:
    def setup_method(self) -> None:
        self.cmd = WhatsUpCommand()

    @patch("commands.whats_up_command.get_command_center_url", return_value="http://localhost:7703")
    @patch("commands.whats_up_command.JarvisCommandCenterClient")
    def test_run_composes_via_llm(self, mock_client_cls: MagicMock, mock_url: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.chat_text.return_value = "Here's what's happening: big news today."
        mock_client_cls.return_value = mock_client

        alerts_data = [_make_alert("Big news").to_dict()]
        response = self.cmd.run(
            _make_request_info(),
            alerts_json=json.dumps(alerts_data),
        )

        assert response.success
        assert "big news" in response.context_data["message"].lower()

    def test_run_empty_alerts(self) -> None:
        response = self.cmd.run(_make_request_info(), alerts_json="[]")
        assert response.success
        assert "no pending" in response.context_data["message"].lower()

    @patch("commands.whats_up_command.get_command_center_url", return_value="http://localhost:7703")
    @patch("commands.whats_up_command.JarvisCommandCenterClient")
    def test_run_fallback_on_llm_failure(self, mock_client_cls: MagicMock, mock_url: MagicMock) -> None:
        mock_client = MagicMock()
        mock_client.chat_text.return_value = None
        mock_client_cls.return_value = mock_client

        alerts_data = [_make_alert("Storm warning").to_dict()]
        response = self.cmd.run(
            _make_request_info(),
            alerts_json=json.dumps(alerts_data),
        )

        assert response.success
        assert "Storm warning" in response.context_data["message"]


class TestWhatsUpDismiss:
    """Silent-dismiss path — 'clear alerts', 'dismiss notifications' etc."""

    def setup_method(self) -> None:
        self.cmd = WhatsUpCommand()
        self.queue = AlertQueueService()

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_dismiss_flushes_queue_silently(self, mock_get_queue: MagicMock) -> None:
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        self.queue.add_alert(_make_alert("Other", priority=3))
        mock_get_queue.return_value = self.queue

        result = self.cmd.pre_route("clear alerts")
        assert result is not None
        assert result.arguments["dismissed"] is True
        assert result.arguments["dismissed_count"] == 2
        assert self.queue.count() == 0
        # alerts_json must NOT be set on the dismiss path (run() short-
        # circuits on the dismissed flag).
        assert "alerts_json" not in result.arguments

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_dismiss_with_empty_queue_still_matches(self, mock_get_queue: MagicMock) -> None:
        """Unlike the greeting fast-path, dismiss is valid even with
        an empty queue — clearing nothing is still a clear intent."""
        mock_get_queue.return_value = self.queue  # empty

        result = self.cmd.pre_route("dismiss notifications")
        assert result is not None
        assert result.arguments["dismissed"] is True
        assert result.arguments["dismissed_count"] == 0

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_dismiss_phrase_variants(self, mock_get_queue: MagicMock) -> None:
        mock_get_queue.return_value = self.queue

        for phrase in [
            "clear alerts",
            "clear notifications",
            "dismiss alerts",
            "dismiss notifications",
            "clear all alerts",
            "cancel notifications",
        ]:
            result = self.cmd.pre_route(phrase)
            assert result is not None, f"phrase did not match: {phrase!r}"
            assert result.arguments["dismissed"] is True

    @patch("commands.whats_up_command.get_alert_queue_service")
    def test_dismiss_precedence_over_greeting(self, mock_get_queue: MagicMock) -> None:
        """'clear notifications' contains 'notifications', which is a
        greeting trigger substring. The dismiss path must win."""
        self.queue.add_alert(_make_alert("Reminder", priority=3))
        mock_get_queue.return_value = self.queue

        result = self.cmd.pre_route("clear notifications")
        assert result is not None
        assert result.arguments.get("dismissed") is True
        # Greeting path would set alerts_json; dismiss path does not.
        assert "alerts_json" not in result.arguments

    def test_dismiss_disabled_pattern_falls_through_to_greeting(self) -> None:
        """If only the dismiss fast-path is disabled, the greeting
        check still runs and 'clear notifications' won't match any
        greeting phrase — so we return None."""
        result = self.cmd.pre_route(
            "clear alerts",
            disabled_pattern_ids={"check_alerts.dismiss"},
        )
        # No greeting trigger matches 'clear alerts', no fall-through.
        assert result is None

    def test_run_dismissed_returns_brief_confirmation(self) -> None:
        response = self.cmd.run(
            _make_request_info(),
            dismissed=True,
            dismissed_count=3,
        )
        assert response.success
        assert response.context_data["message"] == "Cleared."


class TestWhatsUpMetadata:
    def test_command_name(self) -> None:
        cmd = WhatsUpCommand()
        assert cmd.command_name == "check_alerts"

    def test_has_keywords(self) -> None:
        cmd = WhatsUpCommand()
        assert "alerts" in cmd.keywords

    def test_has_examples(self) -> None:
        cmd = WhatsUpCommand()
        examples = cmd.generate_prompt_examples()
        assert len(examples) > 0
