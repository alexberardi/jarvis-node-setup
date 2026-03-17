"""Tests for EmailCommand — list, read, search, send, reply, archive, trash, star."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from commands.email_command import EmailCommand
from jarvis_services.email_message import EmailMessage


@pytest.fixture
def command():
    return EmailCommand()


def _make_emails(count: int = 3) -> list[EmailMessage]:
    return [
        EmailMessage(
            id=f"msg{i}",
            thread_id=f"thread-msg{i}",
            sender=f"User{i} <user{i}@test.com>",
            sender_name=f"User{i}",
            subject=f"Subject {i}",
            snippet=f"Preview of email {i}",
            date=datetime(2026, 3, 14, 10 + i, 0, 0),
            is_unread=True,
        )
        for i in range(count)
    ]


def _mock_secrets(key: str, scope: str) -> str | None:
    return {
        "GMAIL_ACCESS_TOKEN": "tok",
        "GMAIL_REFRESH_TOKEN": "ref",
        "GMAIL_CLIENT_ID": "cid",
    }.get(key)


class TestProperties:
    def test_command_name(self, command: EmailCommand):
        assert command.command_name == "email"

    def test_description_mentions_email(self, command: EmailCommand):
        assert "email" in command.description.lower()

    def test_keywords_include_email_variants(self, command: EmailCommand):
        kw = command.keywords
        for word in ["email", "inbox", "gmail", "read", "send"]:
            assert word in kw

    def test_has_action_parameter(self, command: EmailCommand):
        names = [p.name for p in command.parameters]
        assert "action" in names

    def test_has_email_index_parameter(self, command: EmailCommand):
        names = [p.name for p in command.parameters]
        assert "email_index" in names

    def test_has_max_results_parameter(self, command: EmailCommand):
        names = [p.name for p in command.parameters]
        assert "max_results" in names

    @patch("commands.email_command.get_email_provider", return_value="gmail")
    def test_associated_service_gmail(self, _mock, command: EmailCommand):
        assert command.associated_service == "Gmail"

    @patch("commands.email_command.get_email_provider", return_value="imap")
    def test_associated_service_imap(self, _mock, command: EmailCommand):
        assert command.associated_service == "IMAP Email"


class TestSecrets:
    @patch("commands.email_command.get_email_provider", return_value="gmail")
    def test_gmail_required_secrets_includes_client_id(self, _mock, command: EmailCommand):
        keys = [s.key for s in command.required_secrets]
        assert "GMAIL_CLIENT_ID" in keys
        assert "EMAIL_PROVIDER" in keys

    @patch("commands.email_command.get_email_provider", return_value="imap")
    def test_imap_required_secrets_includes_credentials(self, _mock, command: EmailCommand):
        keys = [s.key for s in command.required_secrets]
        assert "IMAP_USERNAME" in keys
        assert "IMAP_PASSWORD" in keys
        assert "EMAIL_PROVIDER" in keys

    def test_all_possible_secrets_includes_both_providers(self, command: EmailCommand):
        keys = [s.key for s in command.all_possible_secrets]
        assert "GMAIL_ACCESS_TOKEN" in keys
        assert "GMAIL_REFRESH_TOKEN" in keys
        assert "IMAP_USERNAME" in keys
        assert "IMAP_PASSWORD" in keys
        assert "EMAIL_PROVIDER" in keys


class TestAuthentication:
    @patch("commands.email_command._DEFAULT_CLIENT_ID", "default-client-id")
    @patch("commands.email_command.get_secret_value", return_value=None)
    def test_uses_default_client_id(self, _mock, command: EmailCommand):
        auth = command.authentication
        assert auth is not None
        assert auth.client_id == "default-client-id"

    @patch("commands.email_command.get_secret_value", return_value="custom-client-id")
    def test_user_override_takes_precedence(self, _mock, command: EmailCommand):
        auth = command.authentication
        assert auth is not None
        assert auth.client_id == "custom-client-id"

    @patch("commands.email_command._DEFAULT_CLIENT_ID", "default-client-id")
    @patch("commands.email_command.get_secret_value", return_value=None)
    def test_returns_google_gmail_config(self, _mock, command: EmailCommand):
        auth = command.authentication
        assert auth is not None
        assert auth.provider == "google_gmail"
        assert auth.type == "oauth"

    @patch("commands.email_command._DEFAULT_CLIENT_ID", "default-client-id")
    @patch("commands.email_command.get_secret_value", return_value=None)
    def test_native_redirect_uri_is_set(self, _mock, command: EmailCommand):
        auth = command.authentication
        assert auth.native_redirect_uri is not None
        assert ":/oauthredirect" in auth.native_redirect_uri


class TestStoreAuthValues:
    @patch("services.secret_service.set_secret")
    @patch("services.command_auth_service.clear_auth_flag")
    def test_stores_tokens(self, mock_clear, mock_set, command: EmailCommand):
        command.store_auth_values({
            "access_token": "new-access",
            "refresh_token": "new-refresh",
        })
        mock_set.assert_any_call("GMAIL_ACCESS_TOKEN", "new-access", "integration")
        mock_set.assert_any_call("GMAIL_REFRESH_TOKEN", "new-refresh", "integration")
        mock_clear.assert_called_once_with("google_gmail")


class TestDefaultAction:
    def test_default_action_is_list(self, command: EmailCommand):
        """When no action is provided, default to 'list'."""
        result = command.post_process_tool_call({}, "check my email")
        assert result.get("action") == "list"


class TestPostProcess:
    def test_missing_action_defaults_to_list(self, command: EmailCommand):
        result = command.post_process_tool_call({}, "any new emails?")
        assert result["action"] == "list"

    def test_delete_maps_to_trash(self, command: EmailCommand):
        result = command.post_process_tool_call({"action": "delete"}, "delete email 2")
        assert result["action"] == "trash"

    def test_extracts_ordinal_first(self, command: EmailCommand):
        result = command.post_process_tool_call(
            {"action": "read"}, "read the first email"
        )
        assert result["email_index"] == 1

    def test_extracts_ordinal_third(self, command: EmailCommand):
        result = command.post_process_tool_call(
            {"action": "read"}, "what does the third email say"
        )
        assert result["email_index"] == 3

    def test_extracts_ordinal_fifth(self, command: EmailCommand):
        result = command.post_process_tool_call(
            {"action": "read"}, "read the fifth one"
        )
        assert result["email_index"] == 5

    def test_does_not_overwrite_existing_index(self, command: EmailCommand):
        result = command.post_process_tool_call(
            {"action": "read", "email_index": 2}, "read the first email"
        )
        assert result["email_index"] == 2

    def test_extracts_numeric_index(self, command: EmailCommand):
        result = command.post_process_tool_call(
            {"action": "read"}, "read email 4"
        )
        assert result["email_index"] == 4


# ── Phase 1: List ──────────────────────────────────────────────────────────


class TestListAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_list_returns_numbered_emails(self, _mock, command: EmailCommand):
        emails = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.search.return_value = emails
            response = command.run(MagicMock(), action="list")

        assert response.success is True
        data = response.context_data
        assert data["total_results"] == 3
        assert len(data["emails"]) == 3
        # Each email should have an index field
        assert data["emails"][0]["index"] == 1
        assert data["emails"][2]["index"] == 3

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_list_caches_email_list(self, _mock, command: EmailCommand):
        emails = _make_emails(2)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.search.return_value = emails
            command.run(MagicMock(), action="list")

        assert len(command._last_email_list) == 2
        assert command._last_email_list[0].id == "msg0"

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_list_empty_inbox(self, _mock, command: EmailCommand):
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.search.return_value = []
            response = command.run(MagicMock(), action="list")

        assert response.success is True
        assert response.context_data["total_results"] == 0
        assert response.context_data["emails"] == []

    @patch("commands.email_command.get_secret_value", return_value=None)
    def test_no_auth_returns_error(self, _mock, command: EmailCommand):
        response = command.run(MagicMock(), action="list")
        assert response.success is False
        assert "auth" in response.error_details.lower()


# ── Phase 1: Read ──────────────────────────────────────────────────────────


class TestReadAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_read_by_valid_index(self, _mock, command: EmailCommand):
        # Pre-populate cache
        command._last_email_list = _make_emails(3)

        full_email = EmailMessage(
            id="msg1",
            thread_id="thread-msg1",
            sender="User1 <user1@test.com>",
            sender_name="User1",
            subject="Subject 1",
            snippet="Preview of email 1",
            date=datetime(2026, 3, 14, 11, 0, 0),
            is_unread=True,
            body="Full body of the email with lots of detail...",
        )

        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.fetch_message.return_value = full_email
            response = command.run(MagicMock(), action="read", email_index=2)

        assert response.success is True
        data = response.context_data
        assert data["email"]["subject"] == "Subject 1"
        assert data["email"]["body"] == "Full body of the email with lots of detail..."
        # fetch_message called with correct ID and max_body_chars
        MockCreate.return_value.fetch_message.assert_called_once_with("msg1", max_body_chars=3000)

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_read_index_out_of_range(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(2)
        response = command.run(MagicMock(), action="read", email_index=5)
        assert response.success is False
        assert "out of range" in response.error_details.lower() or "invalid" in response.error_details.lower()

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_read_with_empty_cache(self, _mock, command: EmailCommand):
        command._last_email_list = []
        response = command.run(MagicMock(), action="read", email_index=1)
        assert response.success is False

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_read_index_zero_invalid(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        response = command.run(MagicMock(), action="read", email_index=0)
        assert response.success is False


# ── Phase 2: Search ────────────────────────────────────────────────────────


class TestSearchAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_search_returns_results(self, _mock, command: EmailCommand):
        emails = _make_emails(2)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.search.return_value = emails
            response = command.run(
                MagicMock(), action="search", query="flight confirmation"
            )

        assert response.success is True
        assert response.context_data["total_results"] == 2
        # Verify search was called with the query
        MockCreate.return_value.search.assert_called_once_with(
            "flight confirmation", max_results=5
        )

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_search_caches_results(self, _mock, command: EmailCommand):
        emails = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.search.return_value = emails
            command.run(MagicMock(), action="search", query="meeting")

        assert len(command._last_email_list) == 3

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_search_missing_query(self, _mock, command: EmailCommand):
        response = command.run(MagicMock(), action="search")
        assert response.success is False
        assert "query" in response.error_details.lower()

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_search_then_read(self, _mock, command: EmailCommand):
        """Search results should be readable by index."""
        emails = _make_emails(3)
        full_email = _make_emails(1)[0]
        full_email.body = "Full body content"

        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.search.return_value = emails
            command.run(MagicMock(), action="search", query="test")

            MockCreate.return_value.fetch_message.return_value = full_email
            response = command.run(MagicMock(), action="read", email_index=1)

        assert response.success is True
        assert response.context_data["email"]["body"] == "Full body content"


# ── Phase 3: Send + Reply ─────────────────────────────────────────────────


class TestSendAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_send_returns_preview_with_actions(self, _mock, command: EmailCommand):
        response = command.run(
            MagicMock(),
            action="send",
            to="john@example.com",
            subject="Running late",
            body="I'll be late to the meeting",
        )
        assert response.success is True
        data = response.context_data
        assert "draft" in data
        assert data["draft"]["to"] == "john@example.com"
        assert data["draft"]["body"] == "I'll be late to the meeting"
        # Must have actions on the response (IJarvisButton objects)
        assert response.actions is not None
        action_names = [a.button_action for a in response.actions]
        assert "send_click" in action_names
        assert "cancel_click" in action_names

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_send_missing_to(self, _mock, command: EmailCommand):
        response = command.run(MagicMock(), action="send", body="hello")
        assert response.success is False
        assert "to" in response.error_details.lower()

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_send_missing_body(self, _mock, command: EmailCommand):
        response = command.run(MagicMock(), action="send", to="a@b.com")
        assert response.success is False
        assert "body" in response.error_details.lower()

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_handle_send_click_sends_email(self, _mock, command: EmailCommand):
        draft = {"to": "a@b.com", "subject": "Hi", "body": "Hello", "type": "send"}
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.send.return_value = {"id": "sent-1"}
            response = command.handle_action("send_click", {"draft": draft})

        assert response.success is True
        MockCreate.return_value.send.assert_called_once_with("a@b.com", "Hi", "Hello")

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_handle_cancel_click(self, _mock, command: EmailCommand):
        draft = {"to": "a@b.com", "subject": "Hi", "body": "Hello", "type": "send"}
        response = command.handle_action("cancel_click", {"draft": draft})
        assert response.success is True
        assert response.context_data.get("cancelled") is True


class TestReplyAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_reply_returns_preview_with_actions(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            full_email = _make_emails(1)[0]
            full_email.body = "Original message body"
            MockCreate.return_value.fetch_message.return_value = full_email
            response = command.run(
                MagicMock(),
                action="reply",
                email_index=1,
                body="Got it, thanks!",
            )

        assert response.success is True
        data = response.context_data
        assert "draft" in data
        assert data["draft"]["body"] == "Got it, thanks!"
        assert response.actions is not None
        action_names = [a.button_action for a in response.actions]
        assert "send_click" in action_names

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_reply_missing_body(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        response = command.run(MagicMock(), action="reply", email_index=1)
        assert response.success is False

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_reply_missing_index(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        response = command.run(MagicMock(), action="reply", body="Thanks!")
        assert response.success is False

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_reply_handle_send_click(self, _mock, command: EmailCommand):
        draft = {
            "message_id": "msg0",
            "thread_id": "thread-msg0",
            "to": "user0@test.com",
            "subject": "Re: Subject 0",
            "body": "Got it",
            "type": "reply",
        }
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.reply.return_value = {"id": "reply-1"}
            response = command.handle_action("send_click", {"draft": draft})

        assert response.success is True
        MockCreate.return_value.reply.assert_called_once_with(
            "msg0", "thread-msg0", "Got it"
        )


# ── Phase 4: Archive / Trash / Star ───────────────────────────────────────


class TestArchiveAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_archive_by_index(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.archive.return_value = True
            response = command.run(MagicMock(), action="archive", email_index=2)

        assert response.success is True
        MockCreate.return_value.archive.assert_called_once_with("msg1")

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_archive_removes_from_cache(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.archive.return_value = True
            command.run(MagicMock(), action="archive", email_index=2)

        assert len(command._last_email_list) == 2
        ids = [e.id for e in command._last_email_list]
        assert "msg1" not in ids


class TestTrashAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_trash_by_index(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.trash.return_value = True
            response = command.run(MagicMock(), action="trash", email_index=1)

        assert response.success is True
        MockCreate.return_value.trash.assert_called_once_with("msg0")

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_trash_removes_from_cache(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.trash.return_value = True
            command.run(MagicMock(), action="trash", email_index=1)

        assert len(command._last_email_list) == 2


class TestStarAction:
    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_star_by_index(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(3)
        with patch("commands.email_command.create_email_service") as MockCreate:
            MockCreate.return_value.star.return_value = True
            response = command.run(MagicMock(), action="star", email_index=3)

        assert response.success is True
        MockCreate.return_value.star.assert_called_once_with("msg2")

    @patch("commands.email_command.get_secret_value", side_effect=_mock_secrets)
    def test_invalid_index_errors(self, _mock, command: EmailCommand):
        command._last_email_list = _make_emails(2)
        response = command.run(MagicMock(), action="star", email_index=5)
        assert response.success is False


# ── Examples ───────────────────────────────────────────────────────────────


class TestExamples:
    def test_prompt_examples_have_one_primary(self, command: EmailCommand):
        examples = command.generate_prompt_examples()
        primary = [e for e in examples if e.is_primary]
        assert len(primary) == 1

    def test_adapter_examples_cover_all_actions(self, command: EmailCommand):
        examples = command.generate_adapter_examples()
        actions_seen = set()
        for ex in examples:
            action = ex.expected_parameters.get("action", "list")
            actions_seen.add(action)
        for action in ["list", "read", "search", "send", "reply", "archive", "trash", "star"]:
            assert action in actions_seen, f"Missing adapter example for action={action}"

    def test_adapter_examples_count(self, command: EmailCommand):
        examples = command.generate_adapter_examples()
        assert len(examples) >= 40
