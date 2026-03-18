"""Tests for shared email_message module — EmailMessage dataclass + utility functions."""

from datetime import datetime

from jarvis_services.email_message import EmailMessage, extract_email, extract_name


class TestExtractEmail:
    def test_angle_bracket_format(self) -> None:
        assert extract_email("John Doe <john@example.com>") == "john@example.com"

    def test_quoted_name_format(self) -> None:
        assert extract_email('"Jane Smith" <jane@x.com>') == "jane@x.com"

    def test_bare_email(self) -> None:
        assert extract_email("plain@example.com") == "plain@example.com"

    def test_whitespace_stripped(self) -> None:
        assert extract_email("  user@test.com  ") == "user@test.com"

    def test_empty_string(self) -> None:
        assert extract_email("") == ""


class TestExtractName:
    def test_name_with_email(self) -> None:
        assert extract_name("John Doe <john@example.com>") == "John Doe"

    def test_quoted_name(self) -> None:
        assert extract_name('"Jane Smith" <jane@x.com>') == "Jane Smith"

    def test_bare_email_returns_email(self) -> None:
        assert extract_name("plain@example.com") == "plain@example.com"

    def test_whitespace_stripped(self) -> None:
        assert extract_name("  Bob Jones  <bob@x.com>") == "Bob Jones"

    def test_empty_string(self) -> None:
        assert extract_name("") == ""


class TestEmailMessage:
    def test_dataclass_fields(self) -> None:
        msg = EmailMessage(
            id="123",
            sender="Alice <alice@x.com>",
            sender_name="Alice",
            subject="Test",
            snippet="Preview",
            date=datetime(2026, 3, 15, 10, 0),
            is_unread=True,
        )
        assert msg.id == "123"
        assert msg.body == ""
        assert msg.thread_id == ""

    def test_optional_fields(self) -> None:
        msg = EmailMessage(
            id="456",
            sender="Bob <bob@x.com>",
            sender_name="Bob",
            subject="Hello",
            snippet="Hi there",
            date=datetime(2026, 3, 15),
            is_unread=False,
            body="Full body text",
            thread_id="thread-abc",
        )
        assert msg.body == "Full body text"
        assert msg.thread_id == "thread-abc"
