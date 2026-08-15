"""Tests for the act_on_items follow-up mechanism (node side).

Covers:
- the ActOnItemsCommand tool schema (so the LLM can call it),
- tool_result_formatter wiring referenceable_items onto the wire,
- CommandExecutionService remembering surfaced items (ref_id -> owner/attrs),
- _dispatch_act_on_items resolving ref_ids and calling the owning command's
  @callback with the mobile-identical {action, selected, context} payload,
- the full _execute_tools flow: surface -> record -> act.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from clients.responses.jarvis_command_center import ToolCall, ToolCallFunction
from commands.act_on_items_command import ActOnItemsCommand
from core.command_response import CommandResponse
from jarvis_command_sdk import ReferenceableItem
from utils.command_execution_service import CommandExecutionService
from utils.tool_result_formatter import format_tool_result


def _tc(name: str, args: str = "{}", call_id: str = "tc-1") -> ToolCall:
    return ToolCall(id=call_id, type="function", function=ToolCallFunction(name=name, arguments=args))


@pytest.fixture
def mock_deps():
    """Patch CommandExecutionService dependencies (mirrors test_command_execution_service)."""
    with (
        patch("utils.command_execution_service.get_command_center_url", return_value="http://localhost:7703"),
        patch("utils.command_execution_service.Config") as mock_config,
        patch("utils.command_execution_service.get_command_discovery_service") as mock_discovery_fn,
        patch("utils.command_execution_service.JarvisCommandCenterClient") as mock_client_cls,
    ):
        mock_config.get_str.return_value = "test-node"
        mock_discovery = MagicMock()
        mock_discovery.get_all_commands.return_value = {}
        mock_discovery_fn.return_value = mock_discovery
        mock_client_cls.return_value = MagicMock()
        yield {"config": mock_config, "discovery": mock_discovery}


# ── ActOnItemsCommand schema ─────────────────────────────────────────────────


def test_act_on_items_tool_schema():
    cmd = ActOnItemsCommand()
    assert cmd.command_name == "act_on_items"
    schema = cmd.to_openai_tool_schema()
    props = schema["function"]["parameters"]["properties"]
    assert props["action"]["type"] == "string"
    assert props["ref_ids"]["type"] == "array"
    assert set(schema["function"]["parameters"]["required"]) == {"action", "ref_ids"}


def test_act_on_items_run_is_graceful_fallback():
    cmd = ActOnItemsCommand()
    from core.request_information import RequestInformation

    resp = cmd.run(RequestInformation(voice_command="x", conversation_id="c"))
    assert resp.success
    assert "act on" in resp.context_data["message"].lower()


# ── tool_result_formatter wire-out ───────────────────────────────────────────


def test_format_tool_result_emits_referenceable_items():
    resp = CommandResponse.with_items(
        message="You have 2 unread emails.",
        items=[
            ReferenceableItem(
                ref_id="eml_1", label="from ABC", attrs={"sender": "abc@x.com"}, actions=["mark_read"]
            )
        ],
    )
    out = format_tool_result("tc-1", resp)["output"]
    assert out["message"] == "You have 2 unread emails."
    assert out["referenceable_items"] == [
        {"ref_id": "eml_1", "label": "from ABC", "attrs": {"sender": "abc@x.com"}, "actions": ["mark_read"]}
    ]


def test_format_tool_result_omits_when_no_items():
    resp = CommandResponse.success_response(context_data={"message": "hi"})
    assert "referenceable_items" not in format_tool_result("tc-1", resp)["output"]


# ── surfaced-items memory ────────────────────────────────────────────────────


class TestRecordItems:
    def test_record_populates_buffer(self, mock_deps):
        service = CommandExecutionService()
        service._record_referenceable_items(
            "get_email",
            [ReferenceableItem(ref_id="eml_1", label="from ABC", attrs={"sender": "abc"}, actions=["mark_read"])],
        )
        meta = service._recent_items["eml_1"]
        assert meta["owner"] == "get_email"
        assert meta["actions"] == ["mark_read"]
        assert meta["attrs"] == {"sender": "abc"}

    def test_latest_surfacing_replaces_buffer(self, mock_deps):
        service = CommandExecutionService()
        service._record_referenceable_items("get_email", [ReferenceableItem(ref_id="eml_1", label="l")])
        service._record_referenceable_items("get_news", [ReferenceableItem(ref_id="art_1", label="h")])
        assert "eml_1" not in service._recent_items
        assert "art_1" in service._recent_items

    def test_maybe_record_from_response(self, mock_deps):
        service = CommandExecutionService()
        resp = CommandResponse.with_items(
            message="x", items=[ReferenceableItem(ref_id="eml_1", label="l", actions=["mark_read"])]
        )
        service._maybe_record_items("get_email", resp)
        assert "eml_1" in service._recent_items

    def test_maybe_record_noop_without_items(self, mock_deps):
        service = CommandExecutionService()
        service._maybe_record_items("chat", CommandResponse.success_response())
        assert service._recent_items == {}

    def test_maybe_record_skips_failed_response(self, mock_deps):
        service = CommandExecutionService()
        resp = CommandResponse.error_response(error_details="boom")
        resp.referenceable_items = [ReferenceableItem(ref_id="x", label="l")]
        service._maybe_record_items("get_email", resp)
        assert service._recent_items == {}

    def test_wire_returns_items_when_fresh(self, mock_deps):
        service = CommandExecutionService()
        service._record_referenceable_items(
            "get_email",
            [ReferenceableItem(ref_id="eml_1", label="from ABC", attrs={"sender": "abc"}, actions=["mark_read"])],
        )
        assert service.recently_shown_items_wire() == [
            {"ref_id": "eml_1", "label": "from ABC", "attrs": {"sender": "abc"}, "actions": ["mark_read"]}
        ]

    def test_wire_none_when_empty(self, mock_deps):
        assert CommandExecutionService().recently_shown_items_wire() is None

    def test_wire_none_when_stale(self, mock_deps):
        service = CommandExecutionService()
        service._record_referenceable_items("get_email", [ReferenceableItem(ref_id="eml_1", label="l")])
        service._recent_items_ts -= 10_000  # age past the TTL
        assert service.recently_shown_items_wire() is None
        # And a stale buffer also yields nothing to act on.
        assert service._recent_items_fresh() is False


# ── _dispatch_act_on_items ───────────────────────────────────────────────────


class TestDispatch:
    def _service_with_owner(self, mock_deps, callbacks: dict):
        service = CommandExecutionService()
        owner_cmd = MagicMock()
        owner_cmd.get_callbacks.return_value = callbacks
        mock_deps["discovery"].get_command.return_value = owner_cmd
        return service

    def test_happy_path_calls_callback_with_mobile_shape(self, mock_deps):
        captured = {}

        def mark_read(data, request_info):
            captured["data"] = data
            captured["user_id"] = request_info.user_id
            return CommandResponse.success_response(
                context_data={"message": f"Marked {len(data['selected'])} as read."}
            )

        service = self._service_with_owner(mock_deps, {"mark_read": mark_read})
        service._track_conversation_user("conv-1", 7)
        service._record_referenceable_items(
            "get_email",
            [
                ReferenceableItem(ref_id="eml_1", label="from ABC", attrs={"sender": "abc@x.com"}, actions=["mark_read"]),
                ReferenceableItem(ref_id="eml_2", label="from Dana", attrs={"sender": "dana@x.com"}, actions=["mark_read"]),
            ],
        )

        tc = _tc("act_on_items", '{"action":"mark_read","ref_ids":["eml_1","eml_2"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "mark those as read")

        assert resp.success
        assert resp.context_data["message"] == "Marked 2 as read."
        assert captured["data"]["action"] == "mark_read"
        assert captured["data"]["selected"] == [
            {"key": "eml_1", "sender": "abc@x.com"},
            {"key": "eml_2", "sender": "dana@x.com"},
        ]
        assert captured["data"]["context"] == {"source_tool": "get_email"}
        assert captured["user_id"] == 7

    def test_empty_stash_is_graceful(self, mock_deps):
        service = CommandExecutionService()
        tc = _tc("act_on_items", '{"action":"mark_read","ref_ids":["eml_1"]}')
        resp = service._dispatch_act_on_items(tc, "conv-x", "mark those")
        assert resp.success  # final_response, fail-soft
        assert "act on" in resp.context_data["message"].lower()

    def test_unknown_ref_ids_rejected(self, mock_deps):
        service = self._service_with_owner(mock_deps, {"mark_read": lambda d, r: CommandResponse.success_response()})
        service._record_referenceable_items(
            "get_email", [ReferenceableItem(ref_id="eml_1", label="l", actions=["mark_read"])]
        )
        tc = _tc("act_on_items", '{"action":"mark_read","ref_ids":["hallucinated"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "x")
        assert not resp.success

    def test_unsupported_action(self, mock_deps):
        service = self._service_with_owner(mock_deps, {"mark_read": lambda d, r: CommandResponse.success_response()})
        service._record_referenceable_items(
            "get_email", [ReferenceableItem(ref_id="eml_1", label="l", actions=["mark_read"])]
        )
        tc = _tc("act_on_items", '{"action":"star","ref_ids":["eml_1"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "x")
        assert not resp.success
        assert "star" in resp.context_data["message"].lower()

    def test_destructive_action_refused(self, mock_deps):
        service = self._service_with_owner(mock_deps, {"delete": lambda d, r: CommandResponse.success_response()})
        service._record_referenceable_items(
            "get_email", [ReferenceableItem(ref_id="eml_1", label="l", actions=["delete"])]
        )
        tc = _tc("act_on_items", '{"action":"delete","ref_ids":["eml_1"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "delete those")
        assert not resp.success
        assert "app" in resp.context_data["message"].lower()

    # ── confirm_offered unlock (prds/osx-api.md decision #14) ────────────────

    def test_destructive_dispatches_when_all_selected_confirm_offered(self, mock_deps):
        captured = {}

        def send(data, request_info):
            captured["data"] = data
            return CommandResponse.success_response(context_data={"message": "Sent."})

        service = self._service_with_owner(mock_deps, {"send": send})
        service._record_referenceable_items(
            "draft_reply",
            [ReferenceableItem(
                ref_id="draft:c1", label="draft reply to Sarah",
                attrs={"chat_id": "c1", "confirm_offered": True}, actions=["send"],
            )],
        )
        tc = _tc("act_on_items", '{"action":"send","ref_ids":["draft:c1"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "yes, send it")
        assert resp.success
        assert resp.context_data["message"] == "Sent."
        # attrs (incl. the flag) come from the RECORDED store, spread into selected
        assert captured["data"]["selected"] == [
            {"chat_id": "c1", "confirm_offered": True, "key": "draft:c1"}
        ]

    def test_destructive_refused_without_flag_keeps_exact_wording(self, mock_deps):
        service = self._service_with_owner(mock_deps, {"send": lambda d, r: CommandResponse.success_response()})
        service._record_referenceable_items(
            "draft_reply",
            [ReferenceableItem(ref_id="draft:c1", label="l", attrs={"chat_id": "c1"}, actions=["send"])],
        )
        tc = _tc("act_on_items", '{"action":"send","ref_ids":["draft:c1"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "send it")
        assert not resp.success
        assert resp.context_data["message"] == (
            "I can't send those by voice yet — you can do that from the app."
        )

    def test_mixed_selection_one_unflagged_refuses(self, mock_deps):
        called = []
        service = self._service_with_owner(
            mock_deps, {"send": lambda d, r: called.append(d) or CommandResponse.success_response()}
        )
        service._record_referenceable_items(
            "draft_reply",
            [
                ReferenceableItem(ref_id="d1", label="a", attrs={"confirm_offered": True}, actions=["send"]),
                ReferenceableItem(ref_id="d2", label="b", attrs={}, actions=["send"]),
            ],
        )
        tc = _tc("act_on_items", '{"action":"send","ref_ids":["d1","d2"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "send both")
        assert not resp.success
        assert called == []

    def test_flag_in_tool_call_payload_does_not_unlock(self, mock_deps):
        """The LLM cannot fabricate confirmation: only the RECORDED attrs count."""
        called = []
        service = self._service_with_owner(
            mock_deps, {"send": lambda d, r: called.append(d) or CommandResponse.success_response()}
        )
        service._record_referenceable_items(
            "draft_reply",
            [ReferenceableItem(ref_id="d1", label="a", attrs={"chat_id": "c1"}, actions=["send"])],
        )
        tc = _tc(
            "act_on_items",
            '{"action":"send","ref_ids":["d1"],"confirm_offered":true,'
            '"attrs":{"confirm_offered":true},"selected":[{"confirm_offered":true}]}',
        )
        resp = service._dispatch_act_on_items(tc, "conv-1", "send it")
        assert not resp.success
        assert called == []

    def test_non_destructive_action_unaffected_by_missing_flag(self, mock_deps):
        service = self._service_with_owner(
            mock_deps, {"mark_read": lambda d, r: CommandResponse.success_response(context_data={"message": "ok"})}
        )
        service._record_referenceable_items(
            "get_email", [ReferenceableItem(ref_id="e1", label="l", attrs={}, actions=["mark_read"])]
        )
        tc = _tc("act_on_items", '{"action":"mark_read","ref_ids":["e1"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "mark it read")
        assert resp.success

    def test_truthy_but_not_true_flag_refuses(self, mock_deps):
        """Strict `is True`: a stringy 'true' from a sloppy command doesn't unlock."""
        service = self._service_with_owner(mock_deps, {"send": lambda d, r: CommandResponse.success_response()})
        service._record_referenceable_items(
            "draft_reply",
            [ReferenceableItem(ref_id="d1", label="a", attrs={"confirm_offered": "true"}, actions=["send"])],
        )
        tc = _tc("act_on_items", '{"action":"send","ref_ids":["d1"]}')
        resp = service._dispatch_act_on_items(tc, "conv-1", "send it")
        assert not resp.success


# ── full _execute_tools flow: surface -> record -> act ───────────────────────


def test_execute_tools_surface_then_act(mock_deps):
    captured = {}

    def mark_read(data, request_info):
        captured["data"] = data
        return CommandResponse.success_response(
            context_data={"message": f"Marked {len(data['selected'])} as read."}
        )

    owner_cmd = MagicMock()
    owner_cmd.get_callbacks.return_value = {"mark_read": mark_read}
    owner_cmd.post_process_tool_call.side_effect = lambda args, vc: args
    owner_cmd.required_secrets = []
    owner_cmd.execute.return_value = CommandResponse.with_items(
        message="You have 1 unread email.",
        items=[ReferenceableItem(ref_id="eml_1", label="from ABC", attrs={"sender": "abc"}, actions=["mark_read"])],
    )
    mock_deps["discovery"].get_command.return_value = owner_cmd

    service = CommandExecutionService()

    # Turn 1: the surfacing command runs; items get remembered (node-level).
    service._execute_tools([_tc("get_email", "{}")], "conv-1", "what emails do I have")
    assert "eml_1" in service._recent_items

    # Turn 2: act_on_items resolves the ref_id and calls mark_read.
    r2 = service._execute_tools(
        [_tc("act_on_items", '{"action":"mark_read","ref_ids":["eml_1"]}')], "conv-1", "mark it read"
    )
    out = r2.api_results[0]["output"]
    assert out["success"] is True
    assert out["message"] == "Marked 1 as read."
    assert captured["data"]["selected"] == [{"sender": "abc", "key": "eml_1"}]


# ── pre-route path also remembers the surfaced list (the whole point) ─────────


def test_pre_route_records_items_and_flags_pre_routed(mock_deps):
    service = CommandExecutionService()
    service._load_disabled_fast_paths = MagicMock(return_value={})

    cmd = MagicMock()
    cmd.command_name = "email"
    cmd.required_secrets = []
    cmd.pre_route.return_value = SimpleNamespace(
        arguments={"action": "list"}, spoken_response="You have 1 unread email."
    )
    cmd.execute.return_value = CommandResponse.with_items(
        message="You have 1 unread email.",
        items=[ReferenceableItem(ref_id="eml_1", label="from ABC", actions=["mark_read"])],
    )
    mock_deps["discovery"].get_all_commands.return_value = {"email": cmd}

    with patch("utils.command_execution_service._maybe_take_over_music"):
        result = service.try_pre_route("check my email", "conv-1")

    # The follow-up loop must NOT continue this (unregistered) CC conversation...
    assert result is not None
    assert result["pre_routed"] is True
    # ...and the surfaced list is remembered so "mark those as read" still resolves.
    assert "eml_1" in service._recent_items
    assert service.recently_shown_items_wire() == [
        {"ref_id": "eml_1", "label": "from ABC", "attrs": {}, "actions": ["mark_read"]}
    ]
