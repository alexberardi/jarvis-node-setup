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
