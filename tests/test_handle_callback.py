"""Unit tests for the handle_callback MQTT handler.

handle_callback receives an opaque job_id over MQTT, fetches the
{command, callback, data} payload from CC over authenticated HTTPS,
and dispatches to a method decorated with @callback on the resolved
command. Result is posted back to CC.
"""

import sys
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

# Mock sqlcipher3 + db before any project imports — db.py runs create_engine()
# at import time and sqlcipher3 is a C extension not available in macOS dev.
# Same pattern used by tests/test_track1_reliability.py.
_mock_db = MagicMock()
_mock_db.SessionLocal = MagicMock
_mock_db.engine = MagicMock()
if "sqlcipher3" not in sys.modules:
    sys.modules["sqlcipher3"] = MagicMock()
    sys.modules["sqlcipher3.dbapi2"] = MagicMock()
if "db" not in sys.modules:
    sys.modules["db"] = _mock_db

from scripts.mqtt_tts_listener import handle_callback  # noqa: E402


# ── Helpers ─────────────────────────────────────────────────────────────────


class _FakeResponse:
    """Minimal stand-in for CommandResponse — only the fields handle_callback reads."""
    def __init__(
        self,
        success: bool = True,
        context_data: Optional[Dict[str, Any]] = None,
        error_details: Optional[str] = None,
    ) -> None:
        self.success = success
        self.context_data = context_data
        self.error_details = error_details


def _make_cmd_with_callbacks(callback_map: Dict[str, Any]) -> MagicMock:
    """Build a mock command whose get_callbacks() returns the given map."""
    cmd = MagicMock()
    cmd.get_callbacks = MagicMock(return_value=callback_map)
    return cmd


def _patch_env(
    *,
    cc_url: str = "http://cc.local:7703",
    fetched_payload: Optional[Dict[str, Any]] = None,
    command: Any = None,
    posts: Optional[List[Dict[str, Any]]] = None,
):
    """Stack the four patches the handler needs, returning the context list.

    posts (if provided) is mutated to record any RestClient.post calls.
    """
    if posts is None:
        posts = []

    def fake_post(url: str, data: Optional[Dict[str, Any]] = None, **_):
        posts.append({"url": url, "data": data or {}})
        return {"ok": True}

    def fake_get(_url: str, **_kwargs):
        return fetched_payload

    discovery = MagicMock()
    discovery.get_command = MagicMock(return_value=command)

    return [
        patch("utils.service_discovery.get_command_center_url", return_value=cc_url),
        patch("clients.rest_client.RestClient.get", side_effect=fake_get),
        patch("clients.rest_client.RestClient.post", side_effect=fake_post),
        patch(
            "utils.command_discovery_service.get_command_discovery_service",
            return_value=discovery,
        ),
    ]


# ── Tests ───────────────────────────────────────────────────────────────────


class TestHandleCallbackHappyPath:
    def test_dispatches_to_named_callback_method(self):
        expand_actor = MagicMock(
            return_value=_FakeResponse(success=True, context_data={"actor": "Tom Hanks"})
        )
        cmd = _make_cmd_with_callbacks({"expand_actor": expand_actor})
        posts: List[Dict[str, Any]] = []

        patches = _patch_env(
            fetched_payload={
                "command_name": "movie_knowledge",
                "callback_name": "expand_actor",
                "data": {"actor_id": "nm0000158"},
                "user_id": 7,
            },
            command=cmd,
            posts=posts,
        )
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})

        expand_actor.assert_called_once()
        args, _ = expand_actor.call_args
        data, request_info = args
        assert data == {"actor_id": "nm0000158"}
        assert request_info.user_id == 7
        assert request_info.voice_command == "cb:expand_actor"

    def test_posts_success_result_to_cc(self):
        cmd = _make_cmd_with_callbacks({
            "expand_actor": MagicMock(
                return_value=_FakeResponse(success=True, context_data={"message": "ok"})
            )
        })
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(
            fetched_payload={
                "command_name": "movie_knowledge",
                "callback_name": "expand_actor",
                "data": {},
            },
            command=cmd,
            posts=posts,
        )
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})

        assert len(posts) == 1
        assert posts[0]["url"].endswith("/api/v0/callbacks/job-xyz/result")
        assert posts[0]["data"]["success"] is True
        assert posts[0]["data"]["context_data"] == {"message": "ok"}


class TestHandleCallbackEarlyExits:
    def test_missing_request_id_is_noop(self):
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(posts=posts)
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({})  # no request_id
        # No fetch attempted, no result posted.
        assert posts == []

    def test_unresolved_cc_url_is_noop(self):
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(cc_url="", posts=posts)
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})
        assert posts == []

    def test_failed_payload_fetch_is_silent_noop(self):
        """RestClient.get returning None means we don't know what to dispatch — bail without posting."""
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(fetched_payload=None, posts=posts)
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})
        assert posts == []


class TestHandleCallbackErrorPaths:
    def test_unknown_command_posts_failure(self):
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(
            fetched_payload={
                "command_name": "no_such_command",
                "callback_name": "x",
                "data": {},
            },
            command=None,  # discovery.get_command returns None
            posts=posts,
        )
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})

        assert len(posts) == 1
        assert posts[0]["data"]["success"] is False
        assert "not found" in posts[0]["data"]["error"]

    def test_unknown_callback_name_posts_failure(self):
        cmd = _make_cmd_with_callbacks({"expand_actor": MagicMock()})
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(
            fetched_payload={
                "command_name": "movie_knowledge",
                "callback_name": "expand_unicorn",
                "data": {},
            },
            command=cmd,
            posts=posts,
        )
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})

        assert len(posts) == 1
        assert posts[0]["data"]["success"] is False
        assert "expand_unicorn" in posts[0]["data"]["error"]

    def test_missing_command_name_or_callback_name_posts_failure(self):
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(
            fetched_payload={"data": {}},  # no command_name, no callback_name
            posts=posts,
        )
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})

        assert len(posts) == 1
        assert posts[0]["data"]["success"] is False

    def test_command_without_get_callbacks_posts_failure(self):
        """A command from a pre-0.2.0 SDK has no get_callbacks attribute — fail soft."""
        legacy_cmd = MagicMock(spec=["command_name"])  # no get_callbacks
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(
            fetched_payload={
                "command_name": "legacy",
                "callback_name": "x",
                "data": {},
            },
            command=legacy_cmd,
            posts=posts,
        )
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})

        assert len(posts) == 1
        assert posts[0]["data"]["success"] is False
        assert "does not support callbacks" in posts[0]["data"]["error"]

    def test_callback_raising_exception_posts_failure(self):
        def boom(_data, _request_info):
            raise RuntimeError("kaboom")

        cmd = _make_cmd_with_callbacks({"expand_actor": boom})
        posts: List[Dict[str, Any]] = []
        patches = _patch_env(
            fetched_payload={
                "command_name": "movie_knowledge",
                "callback_name": "expand_actor",
                "data": {},
            },
            command=cmd,
            posts=posts,
        )
        with patches[0], patches[1], patches[2], patches[3]:
            handle_callback({"request_id": "job-xyz"})

        assert len(posts) == 1
        assert posts[0]["data"]["success"] is False
        assert "kaboom" in posts[0]["data"]["error"]


class TestCommandHandlerRegistration:
    def test_callback_handler_is_registered(self):
        """Sanity: the dispatch dict picks up 'callback' as a command type."""
        from scripts.mqtt_tts_listener import command_handlers, handle_callback as cb
        assert command_handlers.get("callback") is cb
