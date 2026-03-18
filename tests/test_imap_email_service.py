"""Tests for ImapEmailService — IMAP/SMTP email backend."""

import email as email_lib
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.utils import format_datetime
from unittest.mock import MagicMock, call, patch

import pytest

from jarvis_services.imap_email_service import ImapEmailService


@pytest.fixture
def service() -> ImapEmailService:
    return ImapEmailService(
        imap_host="localhost",
        imap_port=1143,
        smtp_host="localhost",
        smtp_port=1025,
        username="user@proton.me",
        password="bridge-pass",
        use_ssl=False,
        archive_folder="Archive",
        trash_folder="Trash",
    )


def _build_rfc822(
    sender: str = "Alice <alice@example.com>",
    subject: str = "Test Subject",
    body: str = "Hello world",
    message_id: str = "<msg001@example.com>",
    date: datetime | None = None,
) -> bytes:
    """Build a valid RFC822 message as bytes."""
    msg = MIMEText(body)
    msg["From"] = sender
    msg["To"] = "user@proton.me"
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    if date:
        msg["Date"] = format_datetime(date)
    else:
        msg["Date"] = format_datetime(datetime(2026, 3, 15, 10, 0, tzinfo=timezone.utc))
    return msg.as_bytes()


# ── Query translation ──────────────────────────────────────────────────


class TestTranslateQuery:
    def test_unread_inbox(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("is:unread in:inbox")
        assert "UNSEEN" in criteria

    def test_from_filter(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("from:alice@example.com")
        assert 'FROM "alice@example.com"' in criteria

    def test_to_filter(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("to:bob@example.com")
        assert 'TO "bob@example.com"' in criteria

    def test_subject_filter(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("subject:meeting")
        assert 'SUBJECT "meeting"' in criteria

    def test_newer_than(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("newer_than:1d")
        assert any(c.startswith("SINCE") for c in criteria)

    def test_bare_text_becomes_subject(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("flight confirmation")
        assert 'SUBJECT "flight confirmation"' in criteria

    def test_empty_query_returns_all(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("")
        assert "ALL" in criteria

    def test_is_read(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("is:read")
        assert "SEEN" in criteria

    def test_combined_query(self, service: ImapEmailService) -> None:
        criteria = service._translate_query("is:unread in:inbox newer_than:1d")
        assert "UNSEEN" in criteria
        assert any(c.startswith("SINCE") for c in criteria)


# ── IMAP connection ────────────────────────────────────────────────────


class TestConnection:
    @patch("jarvis_services.imap_email_service.imaplib.IMAP4")
    def test_starttls_connection(self, mock_imap_cls: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_imap_cls.return_value = mock_conn

        conn = service._connect_imap()
        mock_imap_cls.assert_called_once_with("localhost", 1143)
        mock_conn.starttls.assert_called_once()
        mock_conn.login.assert_called_once_with("user@proton.me", "bridge-pass")
        assert conn is mock_conn

    @patch("jarvis_services.imap_email_service.imaplib.IMAP4_SSL")
    def test_ssl_connection(self, mock_imap_ssl: MagicMock) -> None:
        svc = ImapEmailService(
            imap_host="mail.example.com", imap_port=993,
            smtp_host="mail.example.com", smtp_port=465,
            username="user@example.com", password="pass",
            use_ssl=True,
        )
        mock_conn = MagicMock()
        mock_imap_ssl.return_value = mock_conn

        conn = svc._connect_imap()
        mock_imap_ssl.assert_called_once_with("mail.example.com", 993)
        mock_conn.login.assert_called_once_with("user@example.com", "pass")
        assert conn is mock_conn


# ── Search ─────────────────────────────────────────────────────────────


class TestSearch:
    @patch.object(ImapEmailService, "_connect_imap")
    def test_search_returns_emails(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"3"])
        mock_conn.search.return_value = ("OK", [b"1 2 3"])

        # Mock _fetch_envelope for each UID
        raw_msg = _build_rfc822()
        flags = b'1 (RFC822.HEADER FLAGS (\\Seen))'

        def fake_fetch(uid, parts):
            return ("OK", [(flags, raw_msg), None])

        mock_conn.fetch.side_effect = fake_fetch

        with patch.object(service, "_fetch_envelope") as mock_env:
            from jarvis_services.email_message import EmailMessage
            mock_env.return_value = EmailMessage(
                id="1", sender="Alice <alice@example.com>", sender_name="Alice",
                subject="Test", snippet="Preview", date=datetime.now(timezone.utc),
                is_unread=True,
            )
            result = service.search("is:unread in:inbox", max_results=10)

        assert len(result) == 3

    @patch.object(ImapEmailService, "_connect_imap")
    def test_search_empty_results(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.search.return_value = ("OK", [b""])

        result = service.search("is:unread in:inbox")
        assert result == []

    @patch.object(ImapEmailService, "_connect_imap")
    def test_search_limits_results(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"5"])
        mock_conn.search.return_value = ("OK", [b"1 2 3 4 5"])

        with patch.object(service, "_fetch_envelope") as mock_env:
            from jarvis_services.email_message import EmailMessage
            mock_env.return_value = EmailMessage(
                id="1", sender="Test <test@x.com>", sender_name="Test",
                subject="Test", snippet="", date=datetime.now(timezone.utc),
                is_unread=True,
            )
            result = service.search("is:unread", max_results=2)

        assert len(result) == 2

    @patch.object(ImapEmailService, "_connect_imap")
    def test_search_error_returns_empty(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_connect.side_effect = ConnectionError("Connection refused")
        result = service.search("is:unread")
        assert result == []


# ── Fetch message ──────────────────────────────────────────────────────


class TestFetchMessage:
    @patch.object(ImapEmailService, "_connect_imap")
    def test_fetch_returns_email(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])

        raw = _build_rfc822(sender="Bob <bob@x.com>", subject="Important", body="Full body here")
        flags = b'1 (RFC822 FLAGS (\\Seen))'
        mock_conn.fetch.return_value = ("OK", [(flags, raw)])

        result = service.fetch_message("1", max_body_chars=500)
        assert result is not None
        assert result.subject == "Important"
        assert "Full body" in result.body
        assert result.sender_name == "Bob"

    @patch.object(ImapEmailService, "_connect_imap")
    def test_fetch_not_found(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"0"])
        mock_conn.fetch.return_value = ("OK", [None])

        result = service.fetch_message("999")
        assert result is None

    @patch.object(ImapEmailService, "_connect_imap")
    def test_fetch_error_returns_none(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_connect.side_effect = ConnectionError("down")
        result = service.fetch_message("1")
        assert result is None


# ── Send ───────────────────────────────────────────────────────────────


class TestSend:
    @patch("jarvis_services.imap_email_service.smtplib.SMTP")
    def test_send_uses_smtp(self, mock_smtp_cls: MagicMock, service: ImapEmailService) -> None:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = service.send("bob@x.com", "Hi", "Hello Bob")

        assert result["status"] == "sent"
        mock_smtp.starttls.assert_called_once()
        mock_smtp.login.assert_called_once_with("user@proton.me", "bridge-pass")
        mock_smtp.send_message.assert_called_once()

    @patch("jarvis_services.imap_email_service.smtplib.SMTP")
    def test_send_returns_message_id(self, mock_smtp_cls: MagicMock, service: ImapEmailService) -> None:
        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = service.send("alice@x.com", "Subject", "Body")
        assert "id" in result


# ── Reply ──────────────────────────────────────────────────────────────


class TestReply:
    @patch("jarvis_services.imap_email_service.smtplib.SMTP")
    @patch.object(ImapEmailService, "fetch_message")
    def test_reply_threads_headers(
        self, mock_fetch: MagicMock, mock_smtp_cls: MagicMock, service: ImapEmailService
    ) -> None:
        from jarvis_services.email_message import EmailMessage
        mock_fetch.return_value = EmailMessage(
            id="1", sender="Alice <alice@x.com>", sender_name="Alice",
            subject="Original Subject", snippet="", date=datetime.now(timezone.utc),
            is_unread=True, thread_id="<orig@x.com>",
        )

        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        result = service.reply("1", "<orig@x.com>", "Got it!")

        assert result["status"] == "sent"
        # Verify the MIME message has threading headers
        sent_msg = mock_smtp.send_message.call_args[0][0]
        assert sent_msg["In-Reply-To"] == "<orig@x.com>"
        assert sent_msg["References"] == "<orig@x.com>"
        assert sent_msg["Subject"] == "Re: Original Subject"

    @patch.object(ImapEmailService, "fetch_message")
    def test_reply_original_not_found(self, mock_fetch: MagicMock, service: ImapEmailService) -> None:
        mock_fetch.return_value = None
        with pytest.raises(RuntimeError, match="Cannot fetch"):
            service.reply("999", "<nope@x.com>", "Reply text")

    @patch("jarvis_services.imap_email_service.smtplib.SMTP")
    @patch.object(ImapEmailService, "fetch_message")
    def test_reply_preserves_re_prefix(
        self, mock_fetch: MagicMock, mock_smtp_cls: MagicMock, service: ImapEmailService
    ) -> None:
        from jarvis_services.email_message import EmailMessage
        mock_fetch.return_value = EmailMessage(
            id="1", sender="Bob <bob@x.com>", sender_name="Bob",
            subject="Re: Already a reply", snippet="", date=datetime.now(timezone.utc),
            is_unread=True, thread_id="<thread@x.com>",
        )

        mock_smtp = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_smtp)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        service.reply("1", "<thread@x.com>", "OK")
        sent_msg = mock_smtp.send_message.call_args[0][0]
        assert sent_msg["Subject"] == "Re: Already a reply"  # no double Re:


# ── Archive / Trash / Star ─────────────────────────────────────────────


class TestArchive:
    @patch.object(ImapEmailService, "_connect_imap")
    def test_archive_copies_and_deletes(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.copy.return_value = ("OK", None)
        mock_conn.store.return_value = ("OK", None)

        result = service.archive("1")

        assert result is True
        mock_conn.copy.assert_called_once_with("1", "Archive")
        mock_conn.store.assert_called_once_with("1", "+FLAGS", "\\Deleted")
        mock_conn.expunge.assert_called_once()

    @patch.object(ImapEmailService, "_connect_imap")
    def test_archive_copy_failure(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.copy.return_value = ("NO", None)

        result = service.archive("1")
        assert result is False


class TestTrash:
    @patch.object(ImapEmailService, "_connect_imap")
    def test_trash_copies_and_deletes(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.copy.return_value = ("OK", None)
        mock_conn.store.return_value = ("OK", None)

        result = service.trash("1")

        assert result is True
        mock_conn.copy.assert_called_once_with("1", "Trash")

    @patch.object(ImapEmailService, "_connect_imap")
    def test_trash_error_returns_false(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_connect.side_effect = ConnectionError("down")
        result = service.trash("1")
        assert result is False


class TestStar:
    @patch.object(ImapEmailService, "_connect_imap")
    def test_star_sets_flagged(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.store.return_value = ("OK", None)

        result = service.star("1")
        assert result is True
        mock_conn.store.assert_called_once_with("1", "+FLAGS", "\\Flagged")

    @patch.object(ImapEmailService, "_connect_imap")
    def test_unstar_removes_flagged(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_conn = MagicMock()
        mock_connect.return_value = mock_conn
        mock_conn.select.return_value = ("OK", [b"1"])
        mock_conn.store.return_value = ("OK", None)

        result = service.unstar("1")
        assert result is True
        mock_conn.store.assert_called_once_with("1", "-FLAGS", "\\Flagged")

    @patch.object(ImapEmailService, "_connect_imap")
    def test_star_error_returns_false(self, mock_connect: MagicMock, service: ImapEmailService) -> None:
        mock_connect.side_effect = ConnectionError("down")
        result = service.star("1")
        assert result is False


# ── Body extraction ────────────────────────────────────────────────────


class TestExtractBody:
    def test_plain_text(self) -> None:
        msg = MIMEText("Hello world")
        result = ImapEmailService._extract_body(msg)
        assert "Hello world" in result

    def test_truncation(self) -> None:
        long_text = "A" * 2000
        msg = MIMEText(long_text)
        result = ImapEmailService._extract_body(msg, max_chars=100)
        assert len(result) <= 104  # 100 + "..."

    def test_html_fallback(self) -> None:
        msg = MIMEText("<p>Hello <b>world</b></p>", "html")
        result = ImapEmailService._extract_body(msg)
        assert "Hello" in result
        assert "<p>" not in result
