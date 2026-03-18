"""Tests for GoogleGmailService."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import httpx
import pytest

from jarvis_services.google_gmail_service import EmailMessage, GoogleGmailService


@pytest.fixture
def service():
    return GoogleGmailService(
        access_token="test-access-token",
        refresh_token="test-refresh-token",
        client_id="test-client-id",
    )


def _mock_list_response(message_ids: list[str]) -> httpx.Response:
    """Create a mock Gmail list response."""
    messages = [{"id": mid, "threadId": f"thread-{mid}"} for mid in message_ids]
    return httpx.Response(
        200,
        json={"messages": messages, "resultSizeEstimate": len(messages)},
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages"),
    )


def _mock_empty_list_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"resultSizeEstimate": 0},
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages"),
    )


def _mock_message_response(
    msg_id: str,
    sender: str = "Jane Doe <jane@example.com>",
    subject: str = "Test Subject",
    date: str = "Fri, 14 Mar 2026 10:00:00 -0700",
    snippet: str = "This is a preview of the email content...",
    labels: list[str] | None = None,
    thread_id: str = "",
) -> httpx.Response:
    headers = [
        {"name": "From", "value": sender},
        {"name": "Subject", "value": subject},
        {"name": "Date", "value": date},
    ]
    return httpx.Response(
        200,
        json={
            "id": msg_id,
            "threadId": thread_id or f"thread-{msg_id}",
            "labelIds": labels or ["UNREAD", "INBOX"],
            "snippet": snippet,
            "payload": {"headers": headers},
        },
        request=httpx.Request("GET", f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}"),
    )


def _mock_401_response() -> httpx.Response:
    return httpx.Response(
        401,
        json={"error": {"code": 401, "message": "Invalid Credentials"}},
        request=httpx.Request("GET", "https://gmail.googleapis.com/gmail/v1/users/me/messages"),
    )


class TestSearch:
    def test_search_returns_email_messages(self, service: GoogleGmailService):
        responses = [
            _mock_list_response(["msg1", "msg2"]),
            _mock_message_response("msg1", sender="Alice <alice@test.com>", subject="Hello"),
            _mock_message_response("msg2", sender="Bob <bob@test.com>", subject="Meeting"),
        ]

        with patch("jarvis_services.google_gmail_service.httpx.get", side_effect=responses):
            emails = service.search("is:unread in:inbox", max_results=5)

        assert len(emails) == 2
        assert emails[0].sender_name == "Alice"
        assert emails[0].subject == "Hello"
        assert emails[1].sender_name == "Bob"
        assert emails[1].subject == "Meeting"

    def test_search_passes_query(self, service: GoogleGmailService):
        with patch("jarvis_services.google_gmail_service.httpx.get") as mock_get:
            mock_get.return_value = _mock_empty_list_response()
            service.search("from:john subject:meeting", max_results=3)

        call_args = mock_get.call_args_list[0]
        assert call_args.kwargs.get("params", {}).get("q") == "from:john subject:meeting"
        assert call_args.kwargs.get("params", {}).get("maxResults") == 3

    def test_empty_results(self, service: GoogleGmailService):
        with patch(
            "jarvis_services.google_gmail_service.httpx.get",
            return_value=_mock_empty_list_response(),
        ):
            emails = service.search("nonexistent")

        assert emails == []


class TestFetchUnread:
    def test_delegates_to_search(self, service: GoogleGmailService):
        with patch.object(service, "search", return_value=[]) as mock_search:
            service.fetch_unread(max_results=7)

        mock_search.assert_called_once_with("is:unread in:inbox", max_results=7)

    def test_401_flags_reauth(self, service: GoogleGmailService):
        with (
            patch(
                "jarvis_services.google_gmail_service.httpx.get",
                return_value=_mock_401_response(),
            ),
            patch.object(service, "_flag_reauth") as mock_flag,
        ):
            emails = service.search("is:unread in:inbox")

        assert emails == []
        mock_flag.assert_called_once()


class TestFetchMessage:
    def test_returns_message_with_body(self, service: GoogleGmailService):
        with patch("jarvis_services.google_gmail_service.httpx.get") as mock_get:
            mock_get.return_value = _mock_message_response(
                "msg1", sender="Alice <alice@test.com>", subject="Hello"
            )
            result = service.fetch_message("msg1", max_body_chars=3000)

        assert result is not None
        assert result.id == "msg1"
        assert result.sender_name == "Alice"

    def test_returns_none_on_401(self, service: GoogleGmailService):
        with (
            patch("jarvis_services.google_gmail_service.httpx.get", return_value=_mock_401_response()),
            patch.object(service, "_flag_reauth"),
        ):
            result = service.fetch_message("msg1")

        assert result is None


class TestSend:
    def test_send_rfc2822(self, service: GoogleGmailService):
        mock_response = httpx.Response(
            200,
            json={"id": "sent-1", "threadId": "thread-sent-1"},
            request=httpx.Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"),
        )

        with patch("jarvis_services.google_gmail_service.httpx.post", return_value=mock_response) as mock_post:
            result = service.send("bob@test.com", "Hello", "Hi Bob!")

        assert result["id"] == "sent-1"
        call_json = mock_post.call_args.kwargs.get("json", {})
        assert "raw" in call_json
        assert "threadId" not in call_json

    def test_send_403_flags_reauth(self, service: GoogleGmailService):
        mock_response = httpx.Response(
            403,
            json={"error": {"code": 403}},
            request=httpx.Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"),
        )

        with (
            patch("jarvis_services.google_gmail_service.httpx.post", return_value=mock_response),
            patch.object(service, "_flag_reauth") as mock_flag,
            pytest.raises(RuntimeError, match="permission denied"),
        ):
            service.send("bob@test.com", "Hello", "Hi Bob!")

        mock_flag.assert_called_once()


class TestReply:
    def test_reply_includes_thread_id(self, service: GoogleGmailService):
        # First call: fetch original message headers
        original_msg = _mock_message_response(
            "msg1",
            sender="Alice <alice@test.com>",
            subject="Original Subject",
            thread_id="thread-1",
        )

        send_response = httpx.Response(
            200,
            json={"id": "reply-1", "threadId": "thread-1"},
            request=httpx.Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"),
        )

        with (
            patch("jarvis_services.google_gmail_service.httpx.get", return_value=original_msg),
            patch("jarvis_services.google_gmail_service.httpx.post", return_value=send_response) as mock_post,
        ):
            result = service.reply("msg1", "thread-1", "Got it!")

        assert result["id"] == "reply-1"
        call_json = mock_post.call_args.kwargs.get("json", {})
        assert call_json["threadId"] == "thread-1"


class TestModifyLabels:
    def test_archive_removes_inbox(self, service: GoogleGmailService):
        mock_response = httpx.Response(
            200,
            json={"id": "msg1"},
            request=httpx.Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/msg1/modify"),
        )

        with patch("jarvis_services.google_gmail_service.httpx.post", return_value=mock_response) as mock_post:
            result = service.archive("msg1")

        assert result is True
        call_json = mock_post.call_args.kwargs.get("json", {})
        assert "INBOX" in call_json["removeLabelIds"]

    def test_star_adds_starred(self, service: GoogleGmailService):
        mock_response = httpx.Response(
            200,
            json={"id": "msg1"},
            request=httpx.Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/msg1/modify"),
        )

        with patch("jarvis_services.google_gmail_service.httpx.post", return_value=mock_response) as mock_post:
            result = service.star("msg1")

        assert result is True
        call_json = mock_post.call_args.kwargs.get("json", {})
        assert "STARRED" in call_json["addLabelIds"]

    def test_trash_uses_trash_endpoint(self, service: GoogleGmailService):
        mock_response = httpx.Response(
            200,
            json={"id": "msg1"},
            request=httpx.Request("POST", "https://gmail.googleapis.com/gmail/v1/users/me/messages/msg1/trash"),
        )

        with patch("jarvis_services.google_gmail_service.httpx.post", return_value=mock_response) as mock_post:
            result = service.trash("msg1")

        assert result is True
        url = mock_post.call_args.args[0]
        assert "/trash" in url


class TestParseMessage:
    def test_extracts_headers(self, service: GoogleGmailService):
        raw = _mock_message_response(
            "msg1",
            sender="Sarah Connor <sarah@skynet.com>",
            subject="Urgent: Judgment Day",
            snippet="The machines are coming",
        ).json()

        email = service._parse_message(raw)
        assert email.id == "msg1"
        assert email.sender == "Sarah Connor <sarah@skynet.com>"
        assert email.sender_name == "Sarah Connor"
        assert email.subject == "Urgent: Judgment Day"
        assert email.snippet == "The machines are coming"

    def test_includes_thread_id(self, service: GoogleGmailService):
        raw = _mock_message_response("msg1", thread_id="thread-abc").json()
        email = service._parse_message(raw)
        assert email.thread_id == "thread-abc"

    def test_handles_missing_subject(self, service: GoogleGmailService):
        raw = {
            "id": "msg1",
            "threadId": "thread-1",
            "labelIds": ["UNREAD", "INBOX"],
            "snippet": "Preview text",
            "payload": {
                "headers": [
                    {"name": "From", "value": "test@test.com"},
                    {"name": "Date", "value": "Fri, 14 Mar 2026 10:00:00 -0700"},
                ],
            },
        }

        email = service._parse_message(raw)
        assert email.subject == "(no subject)"

    def test_sender_name_parsing_with_angle_brackets(self, service: GoogleGmailService):
        raw = _mock_message_response("msg1", sender="John Doe <john@x.com>").json()
        email = service._parse_message(raw)
        assert email.sender_name == "John Doe"

    def test_sender_name_parsing_email_only(self, service: GoogleGmailService):
        raw = _mock_message_response("msg1", sender="plain@example.com").json()
        email = service._parse_message(raw)
        assert email.sender_name == "plain@example.com"

    def test_sender_name_parsing_quoted(self, service: GoogleGmailService):
        raw = _mock_message_response("msg1", sender='"Jane Smith" <jane@x.com>').json()
        email = service._parse_message(raw)
        assert email.sender_name == "Jane Smith"

    def test_unread_flag_from_labels(self, service: GoogleGmailService):
        raw = _mock_message_response("msg1", labels=["UNREAD", "INBOX"]).json()
        email = service._parse_message(raw)
        assert email.is_unread is True

        raw_read = _mock_message_response("msg1", labels=["INBOX"]).json()
        email_read = service._parse_message(raw_read)
        assert email_read.is_unread is False


class TestExtractEmail:
    def test_from_angle_brackets(self):
        assert GoogleGmailService._extract_email("John <john@x.com>") == "john@x.com"

    def test_plain_email(self):
        assert GoogleGmailService._extract_email("plain@x.com") == "plain@x.com"
