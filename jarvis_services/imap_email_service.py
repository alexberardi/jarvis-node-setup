"""IMAP/SMTP email client — provider-agnostic backend for any standard mail server.

Same public interface as GoogleGmailService. Designed for Proton Mail Bridge,
Fastmail, self-hosted IMAP, etc. Connects per operation (no persistent connections).
"""

import email as email_lib
import imaplib
import re
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parsedate_to_datetime

from jarvis_log_client import JarvisLogger

from jarvis_services.email_message import EmailMessage, extract_name

logger = JarvisLogger(service="jarvis-node")


class ImapEmailService:
    """IMAP/SMTP email client with the same interface as GoogleGmailService."""

    def __init__(
        self,
        imap_host: str,
        imap_port: int,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        use_ssl: bool = False,
        archive_folder: str = "Archive",
        trash_folder: str = "Trash",
    ) -> None:
        self.imap_host = imap_host
        self.imap_port = imap_port
        self.smtp_host = smtp_host
        self.smtp_port = smtp_port
        self.username = username
        self.password = password
        self.use_ssl = use_ssl
        self.archive_folder = archive_folder
        self.trash_folder = trash_folder

    # ── IMAP connection ───────────────────────────────────────────────

    def _connect_imap(self) -> imaplib.IMAP4 | imaplib.IMAP4_SSL:
        """Open an authenticated IMAP connection."""
        if self.use_ssl:
            conn = imaplib.IMAP4_SSL(self.imap_host, self.imap_port)
        else:
            conn = imaplib.IMAP4(self.imap_host, self.imap_port)
            conn.starttls()
        conn.login(self.username, self.password)
        return conn

    # ── Search / List ─────────────────────────────────────────────────

    def search(self, query: str, max_results: int = 10) -> list[EmailMessage]:
        """Search messages matching a query string.

        Translates common Gmail-style queries to IMAP SEARCH criteria:
        - ``is:unread`` → UNSEEN
        - ``in:inbox`` → (selects INBOX)
        - ``newer_than:Nd`` → SINCE <date>
        - ``from:X`` → FROM "X"
        - bare text → OR SUBJECT "text" BODY "text"
        """
        try:
            conn = self._connect_imap()
            try:
                folder = "INBOX"
                if "in:inbox" in query.lower():
                    folder = "INBOX"
                conn.select(folder, readonly=True)

                imap_criteria = self._translate_query(query)
                status, data = conn.search(None, *imap_criteria)
                if status != "OK":
                    return []

                uids = data[0].split()
                if not uids:
                    return []

                # Take the most recent N UIDs (highest = newest)
                uids = uids[-max_results:]
                uids.reverse()

                emails: list[EmailMessage] = []
                for uid in uids:
                    try:
                        msg = self._fetch_envelope(conn, uid)
                        if msg:
                            emails.append(msg)
                    except Exception as e:
                        logger.debug("Skipping unparseable IMAP message", error=str(e))
                        continue

                return emails
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("IMAP search failed", error=str(e))
            return []

    def fetch_unread(self, max_results: int = 10) -> list[EmailMessage]:
        """Fetch recent unread messages from the inbox."""
        return self.search("is:unread in:inbox", max_results=max_results)

    # ── Single message ────────────────────────────────────────────────

    def fetch_message(self, message_id: str, max_body_chars: int = 1000) -> EmailMessage | None:
        """Fetch a single message by UID with full body."""
        try:
            conn = self._connect_imap()
            try:
                conn.select("INBOX", readonly=True)
                status, data = conn.fetch(message_id, "(RFC822 FLAGS)")
                if status != "OK" or not data or data[0] is None:
                    return None
                raw_email = data[0][1]
                flags_data = data[0][0]
                msg = email_lib.message_from_bytes(raw_email)
                return self._parse_message(msg, message_id, flags_data, max_body_chars)
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to fetch IMAP message", message_id=message_id, error=str(e))
            return None

    # ── Send / Reply ──────────────────────────────────────────────────

    def send(self, to: str, subject: str, body: str) -> dict:
        """Send a new email message via SMTP."""
        msg = MIMEText(body)
        msg["From"] = self.username
        msg["To"] = to
        msg["Subject"] = subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()

        self._smtp_send(msg)
        return {"id": msg["Message-ID"], "status": "sent"}

    def reply(self, message_id: str, thread_id: str, body: str) -> dict:
        """Reply to an existing message.

        Args:
            message_id: IMAP UID of the original message.
            thread_id: Message-ID header of the original (used for threading).
            body: Plain-text reply body.
        """
        original = self.fetch_message(message_id, max_body_chars=0)
        if not original:
            raise RuntimeError(f"Cannot fetch original message UID {message_id}")

        reply_to = original.sender
        # Try to extract a clean email from the sender
        if "<" in reply_to:
            from jarvis_services.email_message import extract_email
            reply_to = extract_email(reply_to)

        reply_subject = original.subject
        if not reply_subject.lower().startswith("re:"):
            reply_subject = f"Re: {reply_subject}"

        msg = MIMEText(body)
        msg["From"] = self.username
        msg["To"] = reply_to
        msg["Subject"] = reply_subject
        msg["Date"] = formatdate(localtime=True)
        msg["Message-ID"] = make_msgid()
        if thread_id:
            msg["In-Reply-To"] = thread_id
            msg["References"] = thread_id

        self._smtp_send(msg)
        return {"id": msg["Message-ID"], "status": "sent"}

    # ── Label / folder management ─────────────────────────────────────

    def archive(self, message_id: str) -> bool:
        """Archive a message (COPY to archive folder, then delete from INBOX)."""
        try:
            conn = self._connect_imap()
            try:
                conn.select("INBOX")
                status, _ = conn.copy(message_id, self.archive_folder)
                if status != "OK":
                    logger.error("IMAP COPY to archive failed", message_id=message_id)
                    return False
                conn.store(message_id, "+FLAGS", "\\Deleted")
                conn.expunge()
                return True
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to archive IMAP message", message_id=message_id, error=str(e))
            return False

    def trash(self, message_id: str) -> bool:
        """Move a message to trash folder."""
        try:
            conn = self._connect_imap()
            try:
                conn.select("INBOX")
                status, _ = conn.copy(message_id, self.trash_folder)
                if status != "OK":
                    logger.error("IMAP COPY to trash failed", message_id=message_id)
                    return False
                conn.store(message_id, "+FLAGS", "\\Deleted")
                conn.expunge()
                return True
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to trash IMAP message", message_id=message_id, error=str(e))
            return False

    def star(self, message_id: str) -> bool:
        """Star (flag) a message."""
        return self._set_flag(message_id, "\\Flagged", add=True)

    def unstar(self, message_id: str) -> bool:
        """Remove star (flag) from a message."""
        return self._set_flag(message_id, "\\Flagged", add=False)

    # ── Internal helpers ──────────────────────────────────────────────

    def _set_flag(self, message_id: str, flag: str, *, add: bool) -> bool:
        """Add or remove a flag on a message."""
        try:
            conn = self._connect_imap()
            try:
                conn.select("INBOX")
                op = "+FLAGS" if add else "-FLAGS"
                status, _ = conn.store(message_id, op, flag)
                return status == "OK"
            finally:
                try:
                    conn.logout()
                except Exception:
                    pass
        except Exception as e:
            logger.error("Failed to set IMAP flag", message_id=message_id, flag=flag, error=str(e))
            return False

    def _smtp_send(self, msg: MIMEText) -> None:
        """Send a MIME message via SMTP with STARTTLS."""
        with smtplib.SMTP(self.smtp_host, self.smtp_port) as smtp:
            smtp.starttls()
            smtp.login(self.username, self.password)
            smtp.send_message(msg)

    def _fetch_envelope(self, conn: imaplib.IMAP4, uid: bytes) -> EmailMessage | None:
        """Fetch headers + snippet for a single message (lightweight)."""
        status, data = conn.fetch(uid, "(RFC822.HEADER FLAGS BODY.PEEK[TEXT]<0.200>)")
        if status != "OK" or not data or data[0] is None:
            return None

        # data layout: [(b'UID FLAGS ...', header_bytes), (b'...', snippet_bytes), b')']
        header_bytes = data[0][1]
        flags_data = data[0][0]

        # Snippet from body peek (up to 200 bytes)
        snippet = ""
        if len(data) > 1 and data[1] is not None:
            if isinstance(data[1], tuple) and len(data[1]) > 1:
                snippet_bytes = data[1][1]
                snippet = snippet_bytes.decode("utf-8", errors="replace").strip()
                snippet = re.sub(r"\s+", " ", snippet)[:150]

        msg = email_lib.message_from_bytes(header_bytes)
        uid_str = uid.decode("ascii") if isinstance(uid, bytes) else str(uid)

        sender = msg.get("From", "Unknown")
        subject = msg.get("Subject", "(no subject)")
        message_id_header = msg.get("Message-ID", "")
        date_str = msg.get("Date", "")

        try:
            date = parsedate_to_datetime(date_str) if date_str else datetime.now(timezone.utc)
        except (ValueError, TypeError):
            date = datetime.now(timezone.utc)

        is_unread = b"\\Seen" not in flags_data

        return EmailMessage(
            id=uid_str,
            thread_id=message_id_header,
            sender=sender,
            sender_name=extract_name(sender),
            subject=subject,
            snippet=snippet,
            date=date,
            is_unread=is_unread,
        )

    def _parse_message(
        self,
        msg: email_lib.message.Message,
        uid: str,
        flags_data: bytes,
        max_body_chars: int = 1000,
    ) -> EmailMessage:
        """Parse a full RFC822 message into an EmailMessage."""
        sender = msg.get("From", "Unknown")
        subject = msg.get("Subject", "(no subject)")
        message_id_header = msg.get("Message-ID", "")
        date_str = msg.get("Date", "")

        try:
            date = parsedate_to_datetime(date_str) if date_str else datetime.now(timezone.utc)
        except (ValueError, TypeError):
            date = datetime.now(timezone.utc)

        is_unread = b"\\Seen" not in flags_data

        body = self._extract_body(msg, max_body_chars)
        snippet = re.sub(r"\s+", " ", body[:150]).strip() if body else ""

        return EmailMessage(
            id=uid,
            thread_id=message_id_header,
            sender=sender,
            sender_name=extract_name(sender),
            subject=subject,
            snippet=snippet,
            date=date,
            is_unread=is_unread,
            body=body,
        )

    @staticmethod
    def _extract_body(msg: email_lib.message.Message, max_chars: int = 1000) -> str:
        """Extract plain-text body from a MIME message.

        Walks the MIME tree for text/plain, falls back to text/html with
        tag stripping. Truncates to max_chars.
        """
        plain = ""
        html = ""

        if msg.is_multipart():
            for part in msg.walk():
                content_type = part.get_content_type()
                if content_type == "text/plain" and not plain:
                    payload = part.get_payload(decode=True)
                    if payload:
                        plain = payload.decode("utf-8", errors="replace")
                elif content_type == "text/html" and not html:
                    payload = part.get_payload(decode=True)
                    if payload:
                        html = payload.decode("utf-8", errors="replace")
        else:
            content_type = msg.get_content_type()
            payload = msg.get_payload(decode=True)
            if payload:
                text = payload.decode("utf-8", errors="replace")
                if content_type == "text/plain":
                    plain = text
                elif content_type == "text/html":
                    html = text

        text = plain or ""
        if not text and html:
            text = re.sub(r"<[^>]+>", " ", html)
            text = re.sub(r"\s+", " ", text).strip()

        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if max_chars and len(text) > max_chars:
            text = text[:max_chars] + "..."
        return text

    @staticmethod
    def _translate_query(query: str) -> list[str]:
        """Translate Gmail-style query to IMAP SEARCH criteria list.

        Supported translations:
        - ``is:unread`` → UNSEEN
        - ``in:inbox`` → (ignored, folder already selected)
        - ``newer_than:Nd`` → SINCE <date>
        - ``from:X`` → FROM "X"
        - bare words → OR SUBJECT "word" BODY "word"
        """
        criteria: list[str] = []
        remaining: list[str] = []

        tokens = query.split()
        for token in tokens:
            lower = token.lower()

            if lower == "is:unread":
                criteria.append("UNSEEN")
            elif lower == "is:read":
                criteria.append("SEEN")
            elif lower == "in:inbox":
                continue  # folder already selected
            elif lower.startswith("newer_than:"):
                # e.g. newer_than:1d, newer_than:7d
                match = re.match(r"newer_than:(\d+)d", lower)
                if match:
                    days = int(match.group(1))
                    since = datetime.now(timezone.utc) - timedelta(days=days)
                    criteria.append(f'SINCE {since.strftime("%d-%b-%Y")}')
            elif lower.startswith("from:"):
                value = token[5:]  # preserve original case
                criteria.append(f'FROM "{value}"')
            elif lower.startswith("to:"):
                value = token[3:]
                criteria.append(f'TO "{value}"')
            elif lower.startswith("subject:"):
                value = token[8:]
                criteria.append(f'SUBJECT "{value}"')
            else:
                remaining.append(token)

        # Bare text → SUBJECT search (simple approach; IMAP doesn't have full-text like Gmail)
        if remaining:
            text = " ".join(remaining)
            criteria.append(f'SUBJECT "{text}"')

        if not criteria:
            criteria.append("ALL")

        return criteria
