"""Provider-agnostic email data model and utility functions.

Shared by GoogleGmailService, ImapEmailService, and consumers like
email_command.py and email_alert_agent.py.
"""

import re
from dataclasses import dataclass
from datetime import datetime


@dataclass
class EmailMessage:
    """A single email message with metadata and optional body."""

    id: str
    sender: str  # Full "Name <email>" string
    sender_name: str  # Parsed display name
    subject: str
    snippet: str  # Short preview text
    date: datetime
    is_unread: bool
    body: str = ""  # Plain-text body (truncated for voice)
    thread_id: str = ""  # Thread identifier (Gmail threadId or Message-ID header)


def extract_email(sender: str) -> str:
    """Extract email address from a sender string.

    "John Doe <john@x.com>" -> "john@x.com"
    "plain@example.com" -> "plain@example.com"
    """
    match = re.search(r'<([^>]+)>', sender)
    if match:
        return match.group(1)
    return sender.strip()


def extract_name(sender: str) -> str:
    """Extract display name from a sender string.

    "John Doe <john@x.com>" -> "John Doe"
    '"Jane Smith" <jane@x.com>' -> "Jane Smith"
    "plain@example.com" -> "plain@example.com"
    """
    match = re.match(r'^"?([^"<]+?)"?\s*<', sender)
    if match:
        return match.group(1).strip()
    return sender.strip()
