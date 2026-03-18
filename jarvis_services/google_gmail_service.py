"""Google Gmail REST client using OAuth2 Bearer tokens.

Thin wrapper around the Gmail v1 API for fetching, searching, sending,
and managing email messages. On 401, flags re-auth so the mobile app
prompts the user.
"""

import base64
import re
from datetime import datetime
from email.mime.text import MIMEText
from email.utils import parsedate_to_datetime

import httpx

from jarvis_log_client import JarvisLogger

from jarvis_services.email_message import EmailMessage, extract_email, extract_name

logger = JarvisLogger(service="jarvis-node")

BASE_URL = "https://gmail.googleapis.com/gmail/v1"


class GoogleGmailService:
    """REST client for Gmail v1 API."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
    ) -> None:
        self.access_token = access_token
        self.refresh_token = refresh_token
        self.client_id = client_id

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    # ── Search / List ──────────────────────────────────────────────────

    def search(self, query: str, max_results: int = 10) -> list[EmailMessage]:
        """Search messages matching a Gmail query string.

        Args:
            query: Gmail search query (e.g. "is:unread in:inbox", "from:john").
            max_results: Maximum number of messages to return.

        Returns:
            List of EmailMessage objects, newest first.
        """
        try:
            response = httpx.get(
                f"{BASE_URL}/users/me/messages",
                params={"q": query, "maxResults": max_results},
                headers=self._headers(),
                timeout=15.0,
            )

            if response.status_code == 401:
                logger.warning("Gmail API returned 401 — flagging re-auth")
                self._flag_reauth()
                raise RuntimeError(
                    "Gmail authentication expired. Please re-authenticate Gmail in the app."
                )

            response.raise_for_status()
            data = response.json()

            messages_list = data.get("messages", [])
            if not messages_list:
                return []

            emails: list[EmailMessage] = []
            for msg_ref in messages_list:
                try:
                    detail = self._fetch_message_detail(msg_ref["id"])
                    if detail:
                        emails.append(self._parse_message(detail))
                except Exception as e:
                    logger.debug("Skipping unparseable Gmail message", error=str(e))
                    continue

            return emails

        except httpx.HTTPStatusError as e:
            logger.error("Gmail API error", status_code=e.response.status_code, detail=str(e))
            return []
        except Exception as e:
            logger.error("Gmail request failed", error=str(e))
            return []

    def fetch_unread(self, max_results: int = 10) -> list[EmailMessage]:
        """Fetch recent unread messages from the inbox."""
        return self.search("is:unread in:inbox", max_results=max_results)

    # ── Single message ─────────────────────────────────────────────────

    def fetch_message(self, message_id: str, max_body_chars: int = 1000) -> EmailMessage | None:
        """Fetch a single message with full body.

        Args:
            message_id: Gmail message ID.
            max_body_chars: Maximum body characters to return.

        Returns:
            EmailMessage with body populated, or None on error.
        """
        try:
            detail = self._fetch_message_detail(message_id)
            if not detail:
                return None
            msg = self._parse_message(detail, max_body_chars=max_body_chars)
            return msg
        except Exception as e:
            logger.error("Failed to fetch message", message_id=message_id, error=str(e))
            return None

    # ── Send / Reply ───────────────────────────────────────────────────

    def send(self, to: str, subject: str, body: str) -> dict:
        """Send a new email message.

        Args:
            to: Recipient email address.
            subject: Email subject line.
            body: Plain-text email body.

        Returns:
            Gmail API response dict with message id.
        """
        raw = self._build_raw_message(to, subject, body)
        return self._send_raw(raw)

    def reply(self, message_id: str, thread_id: str, body: str) -> dict:
        """Reply to an existing email message.

        Constructs proper In-Reply-To and References headers for threading.

        Args:
            message_id: Original message ID.
            thread_id: Thread ID for grouping.
            body: Plain-text reply body.
        """
        # Fetch original message headers for threading
        detail = self._fetch_message_detail(message_id)
        if not detail:
            raise RuntimeError(f"Cannot fetch original message {message_id}")

        headers = {
            h["name"].lower(): h["value"]
            for h in detail.get("payload", {}).get("headers", [])
        }

        original_from = headers.get("from", "")
        original_subject = headers.get("subject", "")
        original_message_id_header = headers.get("message-id", "")

        reply_to = headers.get("reply-to", original_from)
        reply_subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"

        raw = self._build_raw_message(
            to=reply_to,
            subject=reply_subject,
            body=body,
            in_reply_to=original_message_id_header,
            references=original_message_id_header,
        )

        return self._send_raw(raw, thread_id=thread_id)

    # ── Label management ───────────────────────────────────────────────

    def modify_labels(
        self,
        message_id: str,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> bool:
        """Modify labels on a message."""
        try:
            response = httpx.post(
                f"{BASE_URL}/users/me/messages/{message_id}/modify",
                json={
                    "addLabelIds": add_labels or [],
                    "removeLabelIds": remove_labels or [],
                },
                headers=self._headers(),
                timeout=15.0,
            )
            if response.status_code == 401:
                self._flag_reauth()
                return False
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error("Failed to modify labels", message_id=message_id, error=str(e))
            return False

    def archive(self, message_id: str) -> bool:
        """Archive a message (remove INBOX label)."""
        return self.modify_labels(message_id, remove_labels=["INBOX"])

    def trash(self, message_id: str) -> bool:
        """Move a message to trash."""
        try:
            response = httpx.post(
                f"{BASE_URL}/users/me/messages/{message_id}/trash",
                headers=self._headers(),
                timeout=15.0,
            )
            if response.status_code == 401:
                self._flag_reauth()
                return False
            response.raise_for_status()
            return True
        except Exception as e:
            logger.error("Failed to trash message", message_id=message_id, error=str(e))
            return False

    def star(self, message_id: str) -> bool:
        """Star a message."""
        return self.modify_labels(message_id, add_labels=["STARRED"])

    def unstar(self, message_id: str) -> bool:
        """Remove star from a message."""
        return self.modify_labels(message_id, remove_labels=["STARRED"])

    # ── Internal helpers ───────────────────────────────────────────────

    def _fetch_message_detail(self, message_id: str) -> dict | None:
        """Fetch full message (headers + body)."""
        response = httpx.get(
            f"{BASE_URL}/users/me/messages/{message_id}",
            params={"format": "full"},
            headers=self._headers(),
            timeout=15.0,
        )

        if response.status_code == 401:
            self._flag_reauth()
            return None

        response.raise_for_status()
        return response.json()

    def _parse_message(self, raw: dict, max_body_chars: int = 1000) -> EmailMessage:
        """Parse a Gmail API message response into an EmailMessage."""
        headers = {
            h["name"].lower(): h["value"]
            for h in raw.get("payload", {}).get("headers", [])
        }

        sender = headers.get("from", "Unknown")
        subject = headers.get("subject", "(no subject)")
        date_str = headers.get("date", "")
        snippet = raw.get("snippet", "")
        labels = raw.get("labelIds", [])

        # Parse date
        try:
            date = parsedate_to_datetime(date_str) if date_str else datetime.now()
        except (ValueError, TypeError):
            date = datetime.now()

        # Extract plain-text body (truncated for voice readability)
        body = self._extract_body(raw.get("payload", {}), max_chars=max_body_chars)

        return EmailMessage(
            id=raw["id"],
            thread_id=raw.get("threadId", ""),
            sender=sender,
            sender_name=extract_name(sender),
            subject=subject,
            snippet=snippet,
            date=date,
            is_unread="UNREAD" in labels,
            body=body,
        )

    @staticmethod
    def _extract_body(payload: dict, max_chars: int = 1000) -> str:
        """Extract plain-text body from a Gmail message payload.

        Walks the MIME tree looking for text/plain parts. Falls back to
        text/html with tag stripping. Truncates to max_chars for voice.
        """
        def _decode_data(data: str) -> str:
            """Decode base64url-encoded Gmail body data."""
            # Gmail uses URL-safe base64 without padding
            return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")

        def _find_text_parts(part: dict) -> tuple[str, str]:
            """Recursively find text/plain and text/html parts."""
            plain = ""
            html = ""
            mime = part.get("mimeType", "")
            body_data = part.get("body", {}).get("data", "")

            if mime == "text/plain" and body_data:
                plain = _decode_data(body_data)
            elif mime == "text/html" and body_data:
                html = _decode_data(body_data)

            for sub in part.get("parts", []):
                sub_plain, sub_html = _find_text_parts(sub)
                if sub_plain and not plain:
                    plain = sub_plain
                if sub_html and not html:
                    html = sub_html

            return plain, html

        try:
            plain, html = _find_text_parts(payload)
            text = plain or ""

            # Fall back to HTML with basic tag stripping
            if not text and html:
                text = re.sub(r"<[^>]+>", " ", html)
                text = re.sub(r"\s+", " ", text).strip()

            # Clean up whitespace and truncate
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if len(text) > max_chars:
                text = text[:max_chars] + "..."
            return text
        except Exception:
            return ""

    @staticmethod
    def _extract_name(sender: str) -> str:
        """Extract display name from a sender string. Delegates to shared helper."""
        return extract_name(sender)

    @staticmethod
    def _extract_email(sender: str) -> str:
        """Extract email address from a sender string. Delegates to shared helper."""
        return extract_email(sender)

    def _build_raw_message(
        self,
        to: str,
        subject: str,
        body: str,
        in_reply_to: str = "",
        references: str = "",
    ) -> str:
        """Build a base64url-encoded RFC 2822 message."""
        msg = MIMEText(body)
        msg["To"] = to
        msg["Subject"] = subject
        if in_reply_to:
            msg["In-Reply-To"] = in_reply_to
        if references:
            msg["References"] = references

        raw_bytes = msg.as_bytes()
        return base64.urlsafe_b64encode(raw_bytes).decode("ascii")

    def _send_raw(self, raw: str, thread_id: str = "") -> dict:
        """Send a raw RFC 2822 message via Gmail API."""
        payload: dict = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        try:
            response = httpx.post(
                f"{BASE_URL}/users/me/messages/send",
                json=payload,
                headers=self._headers(),
                timeout=15.0,
            )

            if response.status_code == 401:
                self._flag_reauth()
                raise RuntimeError(
                    "Gmail authentication expired. Please re-authenticate Gmail in the app."
                )

            if response.status_code == 403:
                self._flag_reauth()
                raise RuntimeError(
                    "Gmail permission denied. You may need to re-authenticate with updated scopes."
                )

            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            logger.error("Gmail send failed", status_code=e.response.status_code, detail=str(e))
            raise

    @staticmethod
    def _flag_reauth() -> None:
        """Flag the google_gmail provider as needing re-authentication."""
        try:
            from services.command_auth_service import set_needs_auth
            set_needs_auth("google_gmail", "401 Unauthorized from Gmail API")
        except Exception:
            pass
