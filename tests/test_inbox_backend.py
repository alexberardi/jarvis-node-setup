"""Tests for NodeInboxBackend — the SDK JarvisInbox → command-center bridge."""

from unittest.mock import MagicMock, patch

import pytest

import jarvis_command_sdk.inbox as sdk_inbox
from jarvis_command_sdk import JarvisInbox

import services.inbox_backend as inbox_backend
from services.inbox_backend import NodeInboxBackend, init_inbox_backend


_CC_URL = "http://cc:7703"


@pytest.fixture(autouse=True)
def _quiet_logger():
    # logger is mocked so failure-path tests don't leave the log client's
    # background flush thread writing to pytest's closed capture streams.
    with patch.object(inbox_backend, "logger", MagicMock()):
        yield


@pytest.fixture(autouse=True)
def _reset_sdk_backend():
    """Restore the SDK's module-level backend after each test."""
    prior = sdk_inbox.get_inbox_backend()
    yield
    sdk_inbox._backend = prior


@pytest.fixture
def backend() -> NodeInboxBackend:
    return NodeInboxBackend()


def _post(backend: NodeInboxBackend, **overrides) -> str:
    kwargs = {"title": "Inbox triage — 3 unread", **overrides}
    return backend.post_inbox_item("email", **kwargs)


class TestPostInboxItem:
    def test_ok(self, backend: NodeInboxBackend):
        with patch(
            "utils.service_discovery.get_command_center_url",
            return_value=_CC_URL,
        ), patch(
            "clients.rest_client.RestClient.post",
            return_value={"id": "abc", "sent": True},
        ) as mock_post:
            result = _post(
                backend,
                summary="Tap to review",
                body="1. one\n2. two\n3. three",
                category="interactive_list",
                metadata={"type": "interactive_list"},
                user_id=42,
                create_push_notification=True,
                target_type="user",
            )

        assert result == "ok"
        mock_post.assert_called_once_with(
            f"{_CC_URL}/api/v0/node/inbox-item",
            data={
                "title": "Inbox triage — 3 unread",
                "summary": "Tap to review",
                "body": "1. one\n2. two\n3. three",
                "category": "interactive_list",
                "metadata": {"type": "interactive_list"},
                "user_id": 42,
                "create_push_notification": True,
                "target_type": "user",
            },
            timeout=5,
        )

    def test_defaults_fill_the_payload(self, backend: NodeInboxBackend):
        with patch(
            "utils.service_discovery.get_command_center_url",
            return_value=_CC_URL,
        ), patch(
            "clients.rest_client.RestClient.post",
            return_value={"sent": True},
        ) as mock_post:
            result = _post(backend)

        assert result == "ok"
        assert mock_post.call_args.kwargs["data"] == {
            "title": "Inbox triage — 3 unread",
            "summary": "",
            "body": "",
            "category": "general",
            "metadata": None,
            "user_id": None,
            "create_push_notification": False,
            "target_type": "household",
        }

    def test_http_error(self, backend: NodeInboxBackend):
        with patch(
            "utils.service_discovery.get_command_center_url",
            return_value=_CC_URL,
        ), patch(
            "clients.rest_client.RestClient.post", return_value=None
        ):
            assert _post(backend) == "http_error"

    @pytest.mark.parametrize("response", [{"sent": False, "id": None}, {}])
    def test_accepted_but_not_delivered_is_http_error(
        self, backend: NodeInboxBackend, response: dict
    ):
        """A 200 with sent=false (no household, notifications down) is a
        failed post — callers like the digest must see a non-ok tag."""
        with patch(
            "utils.service_discovery.get_command_center_url",
            return_value=_CC_URL,
        ), patch(
            "clients.rest_client.RestClient.post", return_value=response
        ):
            assert _post(backend) == "http_error"

    def test_no_cc_url(self, backend: NodeInboxBackend):
        with patch(
            "utils.service_discovery.get_command_center_url", return_value=""
        ), patch("clients.rest_client.RestClient.post") as mock_post:
            assert _post(backend) == "no_cc_url"
        mock_post.assert_not_called()

    @pytest.mark.parametrize("title", ["", "   "])
    def test_empty_title_is_invalid(
        self, backend: NodeInboxBackend, title: str
    ):
        with patch("clients.rest_client.RestClient.post") as mock_post:
            assert _post(backend, title=title) == "invalid"
        mock_post.assert_not_called()


class TestSdkFacadeRouting:
    def test_facade_routes_through_registered_backend(self):
        init_inbox_backend()
        assert isinstance(sdk_inbox.get_inbox_backend(), NodeInboxBackend)

        with patch(
            "utils.service_discovery.get_command_center_url",
            return_value=_CC_URL,
        ), patch(
            "clients.rest_client.RestClient.post",
            return_value={"sent": True},
        ) as mock_post:
            tag = JarvisInbox("email").post(
                title="Reply ready — Bob",
                summary="Re: invoice",
                interactive_elements=[
                    {
                        "id": "send-1",
                        "label": "Send reply",
                        "command": "email",
                        "callback": "send_draft_reply",
                        "data": {"message_id": "m1"},
                    }
                ],
                create_push_notification=True,
            )

        assert tag == "ok"
        sent = mock_post.call_args.kwargs["data"]
        assert sent["title"] == "Reply ready — Bob"
        # The facade merges interactive_elements into metadata before the
        # backend ever sees them — assert they survive to the wire payload.
        assert sent["metadata"]["interactive_elements"][0]["id"] == "send-1"

    def test_facade_failure_tag_passes_through(self):
        init_inbox_backend()
        with patch(
            "utils.service_discovery.get_command_center_url", return_value=""
        ):
            assert JarvisInbox("email").post(title="x") == "no_cc_url"
