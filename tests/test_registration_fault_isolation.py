"""Tool registration is fault-isolated per command.

Regression guard for the outage where ONE command whose schema failed to build
(a proposable_actions method-vs-@property slip) raised inside start_conversation
and took down registration of ALL 32 tools — the node then sent a malformed
voice command and CC returned 400 ("no response from command center").

start_conversation must now drop only the offending command (from BOTH the
server-side available_commands list and the OpenAI client_tools list, kept
aligned) and register the rest.
"""
from unittest.mock import MagicMock, patch

from clients.jarvis_command_center_client import JarvisCommandCenterClient

REST = "clients.jarvis_command_center_client.RestClient"
TZ = "clients.jarvis_command_center_client.get_user_timezone"


def _cmd(name: str, *, schema_ok: bool = True) -> MagicMock:
    c = MagicMock()
    c.command_name = name
    if schema_ok:
        c.get_command_schema.return_value = {"command_name": name, "keywords": []}
        c.to_openai_tool_schema.return_value = {"function": {"name": name}}
    else:
        # The exact failure shape: schema-building raises for this one command.
        c.get_command_schema.side_effect = RuntimeError("bad proposable_actions")
    return c


def _start(commands: dict) -> tuple[bool, dict]:
    client = JarvisCommandCenterClient("http://test")
    with patch(REST) as rest, \
         patch(TZ, return_value="America/New_York"), \
         patch.object(JarvisCommandCenterClient, "_persist_home_context"):
        rest.post.return_value = {"status": "success"}
        ok = client.start_conversation("conv-1", commands, date_context={})
        payload = rest.post.call_args.kwargs["data"]
    return ok, payload


def test_one_broken_command_does_not_sink_registration():
    ok, payload = _start({
        "good1": _cmd("good1"),
        "bad": _cmd("bad", schema_ok=False),
        "good2": _cmd("good2"),
    })
    assert ok is True  # registration still succeeds
    advertised = {c["command_name"] for c in payload["available_commands"]}
    tools = {t["function"]["name"] for t in payload["client_tools"]}
    # The broken command is dropped; the healthy ones survive.
    assert advertised == {"good1", "good2"}
    assert tools == {"good1", "good2"}


def test_both_lists_stay_aligned_when_a_command_is_dropped():
    _, payload = _start({
        "bad": _cmd("bad", schema_ok=False),
        "good": _cmd("good"),
    })
    # A command must never appear in one list but not the other (server prompt
    # schema vs client tool schema) — that mismatch would let the LLM call a tool
    # the node can't execute, or vice versa.
    advertised = {c["command_name"] for c in payload["available_commands"]}
    tools = {t["function"]["name"] for t in payload["client_tools"]}
    assert advertised == tools == {"good"}
    assert len(payload["available_commands"]) == len(payload["client_tools"])


def test_all_healthy_commands_register_unchanged():
    _, payload = _start({"a": _cmd("a"), "b": _cmd("b"), "c": _cmd("c")})
    assert len(payload["available_commands"]) == 3
    assert len(payload["client_tools"]) == 3


def test_failure_in_openai_schema_also_isolated():
    # The other loop-half: get_command_schema succeeds but to_openai_tool_schema
    # raises. The command must still be dropped from BOTH lists (kept aligned),
    # not advertised in one and missing from the other.
    bad = _cmd("halfbad")
    bad.to_openai_tool_schema.side_effect = RuntimeError("bad tool schema")
    _, payload = _start({"halfbad": bad, "good": _cmd("good")})
    advertised = {c["command_name"] for c in payload["available_commands"]}
    tools = {t["function"]["name"] for t in payload["client_tools"]}
    assert advertised == tools == {"good"}


def test_non_dict_schema_returned_without_raising_is_skipped():
    # A command that returns a non-dict schema WITHOUT raising must be dropped,
    # not advertised as garbage and not allowed to throw past the isolation point.
    weird = _cmd("weird")
    weird.get_command_schema.return_value = None  # non-dict, no exception
    _, payload = _start({"weird": weird, "good": _cmd("good")})
    advertised = {c["command_name"] for c in payload["available_commands"]}
    tools = {t["function"]["name"] for t in payload["client_tools"]}
    assert advertised == tools == {"good"}
    assert None not in payload["available_commands"]


def test_all_commands_failing_registers_empty_but_does_not_raise():
    # Total-failure blind spot: every command drops. Must not raise; lists empty.
    ok, payload = _start({
        "a": _cmd("a", schema_ok=False),
        "b": _cmd("b", schema_ok=False),
    })
    assert payload["available_commands"] == []
    assert payload["client_tools"] == []
    # start_conversation still returns cleanly (CC accepts empty toolset); the
    # empty registration is surfaced via error-level logging, not an exception.
    assert ok is True
