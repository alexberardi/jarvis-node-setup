"""Full-featured email command — list, read, search, send, reply, archive, trash, star.

Supports multiple email backends via EMAIL_PROVIDER secret:
- "gmail" (default) — Google Gmail REST API with OAuth2
- "imap" — Generic IMAP/SMTP (Proton Mail Bridge, Fastmail, etc.)
"""

import re
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.ijarvis_authentication import AuthenticationConfig
from core.ijarvis_button import IJarvisButton
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from core.request_information import RequestInformation
from jarvis_services.email_message import EmailMessage, extract_email
from jarvis_services.email_service_factory import create_email_service, get_email_provider
from services.secret_service import get_secret_value

logger = JarvisLogger(service="jarvis-node")

# Default OAuth client ID — shipped with Jarvis so users don't need to create
# their own Google Cloud project. Override via GMAIL_CLIENT_ID secret if needed.
_DEFAULT_CLIENT_ID = "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec.apps.googleusercontent.com"

# Ordinal words → integers for voice ("read the third email")
_ORDINALS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10,
    "1st": 1, "2nd": 2, "3rd": 3, "4th": 4, "5th": 5,
    "6th": 6, "7th": 7, "8th": 8, "9th": 9, "10th": 10,
    "last": -1,
}

_ALL_ACTIONS = ["list", "read", "search", "send", "reply", "archive", "trash", "star"]


class EmailCommand(IJarvisCommand):

    def __init__(self) -> None:
        super().__init__()
        self._last_email_list: list[EmailMessage] = []

    # ── Properties ─────────────────────────────────────────────────────

    @property
    def command_name(self) -> str:
        return "email"

    @property
    def description(self) -> str:
        return (
            "Manage email: list unread, read, search, send, reply, archive, trash, or star. "
            "Use for ALL email and inbox queries."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "email", "emails", "inbox", "mail", "gmail",
            "messages", "unread", "read", "send", "reply",
            "archive", "trash", "delete", "star",
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=False,
                description="Action to perform",
                enum_values=_ALL_ACTIONS,
                default="list",
            ),
            JarvisParameter(
                "email_index", "int", required=False,
                description="1-indexed position from the last email list (for read/reply/archive/trash/star)",
            ),
            JarvisParameter(
                "max_results", "int", required=False,
                description="Maximum number of emails to return (default 5)",
            ),
            JarvisParameter(
                "query", "string", required=False,
                description="Search query (Gmail search syntax, for search action)",
            ),
            JarvisParameter(
                "to", "string", required=False,
                description="Recipient email address (for send action)",
            ),
            JarvisParameter(
                "subject", "string", required=False,
                description="Email subject line (for send action)",
            ),
            JarvisParameter(
                "body", "string", required=False,
                description="Email body text (for send/reply actions)",
            ),
        ]

    @property
    def associated_service(self) -> str:
        provider = get_email_provider()
        return "IMAP Email" if provider == "imap" else "Gmail"

    @property
    def rules(self) -> List[str]:
        return [
            "Default action is 'list' (unread inbox emails)",
            "Use 'read' with email_index to read a specific email from the last list",
            "Use 'search' with a query for specific emails by sender/topic/keyword",
            "Use 'send' with to + body to compose a new email",
            "Use 'reply' with email_index + body to reply to an email",
            "'archive' removes from inbox but keeps the email",
            "'delete' means 'trash' — moves to trash",
            "Email indices are 1-based from the most recent list/search results",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "NEVER send an email without explicit user intent — when in doubt, use 'list' or 'read'",
        ]

    # ── Secrets & Auth ─────────────────────────────────────────────────

    def _get_client_id(self) -> str:
        return get_secret_value("GMAIL_CLIENT_ID", "integration") or _DEFAULT_CLIENT_ID

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        base = [
            JarvisSecret(
                "EMAIL_PROVIDER",
                "Email provider: 'gmail' (default) or 'imap'",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="Email Provider",
            ),
        ]
        provider = get_email_provider()
        if provider == "imap":
            base.extend([
                JarvisSecret(
                    "IMAP_USERNAME", "IMAP/SMTP login username (full email address)",
                    "integration", "string", is_sensitive=False,
                    friendly_name="IMAP Username",
                ),
                JarvisSecret(
                    "IMAP_PASSWORD", "IMAP/SMTP login password",
                    "integration", "string", is_sensitive=True,
                    friendly_name="IMAP Password",
                ),
            ])
        else:
            base.append(
                JarvisSecret(
                    "GMAIL_CLIENT_ID",
                    "Google OAuth client ID for Gmail (optional — a default is provided)",
                    "integration", "string", required=False, is_sensitive=False,
                    friendly_name="Client ID (optional)",
                ),
            )
        return base

    @property
    def all_possible_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "EMAIL_PROVIDER",
                "Email provider: 'gmail' (default) or 'imap'",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="Email Provider",
            ),
            # Gmail secrets
            JarvisSecret(
                "GMAIL_CLIENT_ID",
                "Google OAuth client ID for Gmail (optional — a default is provided)",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="Client ID (optional)",
            ),
            JarvisSecret(
                "GMAIL_ACCESS_TOKEN", "Gmail OAuth access token",
                "integration", "string", friendly_name="Access Token",
            ),
            JarvisSecret(
                "GMAIL_REFRESH_TOKEN", "Gmail OAuth refresh token",
                "integration", "string", friendly_name="Refresh Token",
            ),
            # IMAP secrets
            JarvisSecret(
                "IMAP_HOST", "IMAP server hostname",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="IMAP Host",
            ),
            JarvisSecret(
                "IMAP_PORT", "IMAP server port (1143 for STARTTLS, 993 for SSL)",
                "integration", "int", required=False, is_sensitive=False,
                friendly_name="IMAP Port",
            ),
            JarvisSecret(
                "IMAP_USERNAME", "IMAP/SMTP login username (full email address)",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="IMAP Username",
            ),
            JarvisSecret(
                "IMAP_PASSWORD", "IMAP/SMTP login password",
                "integration", "string", required=False, is_sensitive=True,
                friendly_name="IMAP Password",
            ),
            JarvisSecret(
                "SMTP_HOST", "SMTP server hostname",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="SMTP Host",
            ),
            JarvisSecret(
                "SMTP_PORT", "SMTP server port",
                "integration", "int", required=False, is_sensitive=False,
                friendly_name="SMTP Port",
            ),
            JarvisSecret(
                "IMAP_USE_SSL", "Use SSL instead of STARTTLS for IMAP",
                "integration", "bool", required=False, is_sensitive=False,
                friendly_name="Use SSL",
            ),
            JarvisSecret(
                "IMAP_ARCHIVE_FOLDER", "IMAP folder name for archive (default: Archive)",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="Archive Folder",
            ),
            JarvisSecret(
                "IMAP_TRASH_FOLDER", "IMAP folder name for trash (default: Trash)",
                "integration", "string", required=False, is_sensitive=False,
                friendly_name="Trash Folder",
            ),
        ]

    @property
    def authentication(self) -> AuthenticationConfig | None:
        if get_email_provider() == "imap":
            return None  # IMAP uses username/password secrets, no OAuth
        client_id = self._get_client_id()
        if not client_id:
            return None
        return AuthenticationConfig(
            type="oauth",
            provider="google_gmail",
            friendly_name="Gmail",
            client_id=client_id,
            keys=["access_token", "refresh_token"],
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            exchange_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/gmail.modify"],
            supports_pkce=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            requires_background_refresh=True,
            refresh_token_secret_key="GMAIL_REFRESH_TOKEN",
            native_redirect_uri="com.googleusercontent.apps.683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec:/oauthredirect",
        )

    def store_auth_values(self, values: dict[str, str]) -> None:
        from services.secret_service import set_secret
        from services.command_auth_service import clear_auth_flag

        if "access_token" in values:
            set_secret("GMAIL_ACCESS_TOKEN", values["access_token"], "integration")
        if "refresh_token" in values:
            set_secret("GMAIL_REFRESH_TOKEN", values["refresh_token"], "integration")
        clear_auth_flag("google_gmail")

    # ── Post-process ───────────────────────────────────────────────────

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        # Default action
        if not args.get("action"):
            args["action"] = "list"

        # "delete" → "trash"
        if args.get("action") == "delete":
            args["action"] = "trash"

        # Extract ordinal from voice if email_index not already set
        if not args.get("email_index"):
            idx = self._extract_index_from_voice(voice_command)
            if idx is not None:
                args["email_index"] = idx

        return args

    @staticmethod
    def _extract_index_from_voice(voice: str) -> int | None:
        """Extract email index from voice command text."""
        lower = voice.lower()

        # Check ordinal words
        for word, idx in _ORDINALS.items():
            if word in lower:
                return idx

        # Check "email N" or "number N" patterns
        match = re.search(r'(?:email|number|#)\s*(\d+)', lower)
        if match:
            return int(match.group(1))

        return None

    # ── Examples ───────────────────────────────────────────────────────

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample("Check my email", {"action": "list"}, is_primary=True),
            CommandExample("Read email 3", {"action": "read", "email_index": 3}),
            CommandExample("Search my email for receipts", {"action": "search", "query": "receipts"}),
            CommandExample(
                "Send an email to john@example.com saying I'll be late",
                {"action": "send", "to": "john@example.com", "subject": "Running late", "body": "I'll be late"},
            ),
            CommandExample("Reply to the first email saying thanks", {"action": "reply", "email_index": 1, "body": "Thanks!"}),
            CommandExample("Archive email 2", {"action": "archive", "email_index": 2}),
            CommandExample("Delete the third email", {"action": "trash", "email_index": 3}),
            CommandExample("Star the first email", {"action": "star", "email_index": 1}),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        examples: list[tuple[str, dict[str, Any], bool]] = [
            # List (default)
            ("Check my email", {"action": "list"}, True),
            ("Any new emails?", {"action": "list"}, False),
            ("What's in my inbox?", {"action": "list"}, False),
            ("Do I have any emails?", {"action": "list"}, False),
            ("Read my emails", {"action": "list"}, False),
            ("Show me my inbox", {"action": "list"}, False),
            ("Any new messages?", {"action": "list"}, False),
            ("Check my Gmail", {"action": "list"}, False),
            ("What emails do I have?", {"action": "list"}, False),
            ("Do I have mail?", {"action": "list"}, False),
            # Read
            ("Read email 3", {"action": "read", "email_index": 3}, False),
            ("Read the first email", {"action": "read", "email_index": 1}, False),
            ("What does the second email say?", {"action": "read", "email_index": 2}, False),
            ("Open email number 5", {"action": "read", "email_index": 5}, False),
            ("Show me the third one", {"action": "read", "email_index": 3}, False),
            ("Read the last email", {"action": "read", "email_index": -1}, False),
            # Search
            ("Search my email for flight confirmation", {"action": "search", "query": "flight confirmation"}, False),
            ("Find emails from John", {"action": "search", "query": "from:John"}, False),
            ("Any emails about the meeting?", {"action": "search", "query": "meeting"}, False),
            ("Search for receipts", {"action": "search", "query": "receipts"}, False),
            ("Find that email about the dentist", {"action": "search", "query": "dentist"}, False),
            ("Look for emails from Amazon", {"action": "search", "query": "from:Amazon"}, False),
            ("Search my inbox for invoices", {"action": "search", "query": "invoices"}, False),
            # Send
            ("Send an email to john@example.com saying I'll be late", {"action": "send", "to": "john@example.com", "subject": "Running late", "body": "I'll be late"}, False),
            ("Email sarah@test.com about the project update", {"action": "send", "to": "sarah@test.com", "subject": "Project update", "body": "Here's the project update"}, False),
            ("Send a message to bob@company.com saying the report is ready", {"action": "send", "to": "bob@company.com", "subject": "Report ready", "body": "The report is ready"}, False),
            ("Compose an email to lisa@work.com", {"action": "send", "to": "lisa@work.com", "subject": "", "body": ""}, False),
            ("Write an email to team@company.com about tomorrow's standup", {"action": "send", "to": "team@company.com", "subject": "Tomorrow's standup", "body": "Regarding tomorrow's standup"}, False),
            # Reply
            ("Reply to the first email saying thanks", {"action": "reply", "email_index": 1, "body": "Thanks!"}, False),
            ("Respond to email 2 with I'll be there", {"action": "reply", "email_index": 2, "body": "I'll be there"}, False),
            ("Reply to the third email saying sounds good", {"action": "reply", "email_index": 3, "body": "Sounds good"}, False),
            ("Answer the second email with yes I can make it", {"action": "reply", "email_index": 2, "body": "Yes I can make it"}, False),
            ("Reply to that first one and say I got it", {"action": "reply", "email_index": 1, "body": "I got it"}, False),
            # Archive
            ("Archive email 3", {"action": "archive", "email_index": 3}, False),
            ("Archive the first email", {"action": "archive", "email_index": 1}, False),
            ("Move email 2 to archive", {"action": "archive", "email_index": 2}, False),
            # Trash
            ("Delete email 2", {"action": "trash", "email_index": 2}, False),
            ("Trash the first email", {"action": "trash", "email_index": 1}, False),
            ("Delete the third email", {"action": "trash", "email_index": 3}, False),
            ("Remove email 4", {"action": "trash", "email_index": 4}, False),
            # Star
            ("Star email 1", {"action": "star", "email_index": 1}, False),
            ("Star the second email", {"action": "star", "email_index": 2}, False),
            ("Mark the first email as starred", {"action": "star", "email_index": 1}, False),
        ]
        return [
            CommandExample(voice, params, is_primary)
            for voice, params, is_primary in examples
        ]

    # ── Action handler (for interactive send/reply confirm) ────────────

    def handle_action(self, action_name: str, context: dict[str, Any]) -> CommandResponse:
        """Handle button-tap actions from the mobile app (send confirm)."""
        if action_name == "send_click":
            draft: dict[str, Any] = context.get("draft", {})
            return self._execute_send(draft)

        # cancel_click handled by ABC default
        return super().handle_action(action_name, context)

    def _execute_send(self, draft: dict[str, Any]) -> CommandResponse:
        """Actually send or reply based on draft type."""
        try:
            service = create_email_service()
        except ValueError as e:
            return CommandResponse.error_response(error_details=str(e))

        try:
            draft_type = draft.get("type", "send")
            if draft_type == "reply":
                result = service.reply(
                    draft["message_id"], draft["thread_id"], draft["body"]
                )
            else:
                result = service.send(draft["to"], draft["subject"], draft["body"])

            return CommandResponse.final_response(
                context_data={
                    "sent": True,
                    "message_id": result.get("id", ""),
                    "message": "Email sent successfully.",
                }
            )
        except Exception as e:
            logger.error("Email send failed", error=str(e))
            return CommandResponse.error_response(error_details=str(e))

    # ── Main execution ─────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str = kwargs.get("action", "list")

        # Auth check — provider-aware
        provider = get_email_provider()
        if provider == "imap":
            if not get_secret_value("IMAP_USERNAME", "integration") or not get_secret_value("IMAP_PASSWORD", "integration"):
                return CommandResponse.error_response(
                    error_details="IMAP not configured. Set IMAP_USERNAME and IMAP_PASSWORD secrets.",
                    context_data={"error": "Not configured"},
                )
        else:
            access_token = get_secret_value("GMAIL_ACCESS_TOKEN", "integration")
            if not access_token:
                return CommandResponse.error_response(
                    error_details="Gmail not authenticated. Complete OAuth setup first.",
                    context_data={"error": "Not authenticated"},
                )

        try:
            if action == "list":
                return self._run_list(**kwargs)
            elif action == "read":
                return self._run_read(**kwargs)
            elif action == "search":
                return self._run_search(**kwargs)
            elif action == "send":
                return self._run_send(**kwargs)
            elif action == "reply":
                return self._run_reply(**kwargs)
            elif action == "archive":
                return self._run_archive(**kwargs)
            elif action == "trash":
                return self._run_trash(**kwargs)
            elif action == "star":
                return self._run_star(**kwargs)
            else:
                return CommandResponse.error_response(
                    error_details=f"Unknown email action: {action}"
                )
        except Exception as e:
            logger.error("email command failed", action=action, error=str(e))
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"error": str(e)},
            )

    # ── Action implementations ─────────────────────────────────────────

    def _get_service(self):
        """Construct the email service for the configured provider."""
        return create_email_service()

    def _run_list(self, **kwargs: Any) -> CommandResponse:
        max_results: int = kwargs.get("max_results") or 5
        service = self._get_service()
        emails = service.search("is:unread in:inbox", max_results=max_results)
        self._last_email_list = emails

        formatted = [
            {
                "index": i + 1,
                "id": e.id,
                "sender": e.sender_name,
                "subject": e.subject,
                "snippet": e.snippet,
                "date": e.date.isoformat(),
            }
            for i, e in enumerate(emails)
        ]

        return CommandResponse.follow_up_response(
            context_data={"emails": formatted, "total_results": len(emails)}
        )

    def _run_read(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to read (e.g. 'read email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        full_email = service.fetch_message(msg.id, max_body_chars=3000)
        if not full_email:
            return CommandResponse.error_response(
                error_details="Could not fetch the email. It may have been deleted."
            )

        return CommandResponse.follow_up_response(
            context_data={
                "email": {
                    "sender": full_email.sender_name,
                    "sender_email": full_email.sender,
                    "subject": full_email.subject,
                    "date": full_email.date.isoformat(),
                    "body": full_email.body,
                }
            }
        )

    def _run_search(self, **kwargs: Any) -> CommandResponse:
        query: str | None = kwargs.get("query")
        if not query:
            return CommandResponse.error_response(
                error_details="Please specify a search query (e.g. 'search for meeting notes')."
            )
        max_results: int = kwargs.get("max_results") or 5
        service = self._get_service()
        emails = service.search(query, max_results=max_results)
        self._last_email_list = emails

        formatted = [
            {
                "index": i + 1,
                "id": e.id,
                "sender": e.sender_name,
                "subject": e.subject,
                "snippet": e.snippet,
                "date": e.date.isoformat(),
            }
            for i, e in enumerate(emails)
        ]

        return CommandResponse.follow_up_response(
            context_data={
                "emails": formatted,
                "total_results": len(emails),
                "query": query,
            }
        )

    def _run_send(self, **kwargs: Any) -> CommandResponse:
        to: str | None = kwargs.get("to")
        body: str | None = kwargs.get("body")
        subject: str = kwargs.get("subject") or ""

        if not to:
            return CommandResponse.error_response(
                error_details="Missing 'to' address. Who should I send the email to?"
            )

        # LLMs often put short messages in subject but leave body empty
        if not body and subject:
            body = subject
        if not body:
            return CommandResponse.error_response(
                error_details="Missing email body. What should the email say?"
            )
        draft = {"to": to, "subject": subject, "body": body, "type": "send"}

        resp = CommandResponse.follow_up_response(
            context_data={
                "command_name": "email",
                "draft": draft,
                "preview": f"To: {to}\nSubject: {subject}\n\n{body}",
                "message": f"I've drafted an email to {to}. Tap Send in the app to confirm.",
                "inbox_title": f"Confirm: {subject}" if subject else f"Email to {to}",
                "inbox_summary": f"I've drafted an email to {to}. Tap Send in the app to confirm.",
            }
        )
        resp.actions = [
            IJarvisButton("Send", "send_click", "primary", "send"),
            IJarvisButton("Cancel", "cancel_click", "destructive"),
        ]
        return resp

    def _run_reply(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        body: str | None = kwargs.get("body")

        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to reply to (e.g. 'reply to email 1')."
            )
        if not body:
            return CommandResponse.error_response(
                error_details="Missing reply body. What should I say?"
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        # Fetch full message for reply-to header
        service = self._get_service()
        full_email = service.fetch_message(msg.id, max_body_chars=500)
        if not full_email:
            return CommandResponse.error_response(
                error_details="Could not fetch the original email."
            )

        reply_to = extract_email(full_email.sender)
        reply_subject = (
            full_email.subject
            if full_email.subject.lower().startswith("re:")
            else f"Re: {full_email.subject}"
        )

        draft = {
            "message_id": msg.id,
            "thread_id": msg.thread_id,
            "to": reply_to,
            "subject": reply_subject,
            "body": body,
            "type": "reply",
        }

        resp = CommandResponse.follow_up_response(
            context_data={
                "command_name": "email",
                "draft": draft,
                "preview": f"Reply to: {full_email.sender_name}\nSubject: {reply_subject}\n\n{body}",
                "message": f"I've drafted a reply to {full_email.sender_name}. Tap Send in the app to confirm.",
                "inbox_title": f"Confirm: {reply_subject}" if reply_subject else f"Reply to {full_email.sender_name}",
                "inbox_summary": f"I've drafted a reply to {full_email.sender_name}. Tap Send in the app to confirm.",
            }
        )
        resp.actions = [
            IJarvisButton("Send", "send_click", "primary", "send"),
            IJarvisButton("Cancel", "cancel_click", "destructive"),
        ]
        return resp

    def _run_archive(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to archive (e.g. 'archive email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.archive(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to archive the email.")

        self._remove_from_cache(msg.id)
        return CommandResponse.final_response(
            context_data={"archived": True, "subject": msg.subject, "message": f"Archived: {msg.subject}"}
        )

    def _run_trash(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to delete (e.g. 'delete email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.trash(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to delete the email.")

        self._remove_from_cache(msg.id)
        return CommandResponse.final_response(
            context_data={"trashed": True, "subject": msg.subject, "message": f"Deleted: {msg.subject}"}
        )

    def _run_star(self, **kwargs: Any) -> CommandResponse:
        email_index: int | None = kwargs.get("email_index")
        if email_index is None:
            return CommandResponse.error_response(
                error_details="Please specify which email to star (e.g. 'star email 1')."
            )

        msg = self._resolve_index(email_index)
        if isinstance(msg, CommandResponse):
            return msg

        service = self._get_service()
        success = service.star(msg.id)
        if not success:
            return CommandResponse.error_response(error_details="Failed to star the email.")

        return CommandResponse.final_response(
            context_data={"starred": True, "subject": msg.subject, "message": f"Starred: {msg.subject}"}
        )

    # ── Helpers ────────────────────────────────────────────────────────

    def _resolve_index(self, email_index: int) -> EmailMessage | CommandResponse:
        """Resolve a 1-based index to an EmailMessage from the cache.

        Returns the EmailMessage on success, or a CommandResponse error.
        """
        if not self._last_email_list:
            return CommandResponse.error_response(
                error_details="No email list available. Try listing or searching emails first."
            )

        # Handle "last" (-1)
        if email_index == -1:
            email_index = len(self._last_email_list)

        if email_index < 1 or email_index > len(self._last_email_list):
            return CommandResponse.error_response(
                error_details=(
                    f"Email index {email_index} is out of range. "
                    f"You have {len(self._last_email_list)} emails in the current list."
                )
            )

        return self._last_email_list[email_index - 1]

    def _remove_from_cache(self, message_id: str) -> None:
        """Remove a message from the cached email list."""
        self._last_email_list = [e for e in self._last_email_list if e.id != message_id]
