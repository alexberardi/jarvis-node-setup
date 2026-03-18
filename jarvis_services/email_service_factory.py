"""Factory for constructing the configured email service backend.

Reads ``EMAIL_PROVIDER`` secret to decide between Gmail (REST API) and
IMAP/SMTP. Used by both ``email_command.py`` and ``email_alert_agent.py``
so provider selection is centralized.
"""

from jarvis_services.google_gmail_service import GoogleGmailService
from jarvis_services.imap_email_service import ImapEmailService
from services.secret_service import get_secret_value

SCOPE = "integration"


def get_email_provider() -> str:
    """Return the configured email provider name (lowercase)."""
    return (get_secret_value("EMAIL_PROVIDER", SCOPE) or "gmail").lower()


def create_email_service() -> GoogleGmailService | ImapEmailService:
    """Construct the email service matching ``EMAIL_PROVIDER`` secret.

    Returns:
        A ``GoogleGmailService`` (default) or ``ImapEmailService`` instance.

    Raises:
        ValueError: If required secrets are missing for the chosen provider.
    """
    provider = get_email_provider()

    if provider == "imap":
        username = get_secret_value("IMAP_USERNAME", SCOPE)
        password = get_secret_value("IMAP_PASSWORD", SCOPE)
        if not username or not password:
            raise ValueError("IMAP_USERNAME and IMAP_PASSWORD secrets are required for IMAP provider")

        return ImapEmailService(
            imap_host=get_secret_value("IMAP_HOST", SCOPE) or "localhost",
            imap_port=int(get_secret_value("IMAP_PORT", SCOPE) or "1143"),
            smtp_host=get_secret_value("SMTP_HOST", SCOPE) or "localhost",
            smtp_port=int(get_secret_value("SMTP_PORT", SCOPE) or "1025"),
            username=username,
            password=password,
            use_ssl=(get_secret_value("IMAP_USE_SSL", SCOPE) or "false").lower() == "true",
            archive_folder=get_secret_value("IMAP_ARCHIVE_FOLDER", SCOPE) or "Archive",
            trash_folder=get_secret_value("IMAP_TRASH_FOLDER", SCOPE) or "Trash",
        )

    # Default: Gmail
    access_token = get_secret_value("GMAIL_ACCESS_TOKEN", SCOPE)
    refresh_token = get_secret_value("GMAIL_REFRESH_TOKEN", SCOPE)
    client_id = get_secret_value("GMAIL_CLIENT_ID", SCOPE)

    return GoogleGmailService(
        access_token=access_token or "",
        refresh_token=refresh_token or "",
        client_id=client_id or "",
    )
