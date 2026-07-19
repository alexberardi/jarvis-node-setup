"""Tests for context_handler — plan-time context queries over MQTT.

The invariant under test throughout: every path publishes a response.
A silent drop reads to command-center as a timeout ("node offline"), which
would make the planner degrade for the wrong reason.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jarvis_command_sdk import (
    CommandExample,
    CommandResponse,
    ContextOperation,
    ContextResult,
    IJarvisCommand,
)

from services import context_handler


# ── Fixtures ───────────────────────────────────────────────────────────────


AVAILABILITY = ContextOperation(
    name="availability",
    description="Free/busy windows",
    params_schema={
        "start": {"type": "string", "required": True, "description": "ISO"},
        "end": {"type": "string", "required": True, "description": "ISO"},
    },
)


class _BaseCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "base"

    @property
    def description(self) -> str:
        return "test"

    @property
    def parameters(self):
        return []

    @property
    def required_secrets(self):
        return []

    @property
    def keywords(self):
        return ["base"]

    def generate_prompt_examples(self):
        return [CommandExample("x", {}, is_primary=True)]

    def generate_adapter_examples(self):
        return self.generate_prompt_examples()

    def run(self, request_info, **kwargs):
        return CommandResponse.success_response({})


class _CalendarCommand(_BaseCommand):
    @property
    def command_name(self) -> str:
        return "calendar"

    @property
    def context_operations(self):
        return [AVAILABILITY]

    def execute_context_operation(self, operation, params):
        return ContextResult(
            data={"busy": ["Thu 15:30-16:00"], "free": ["Thu 14:00-15:30"]}
        )


class _ExplodingCommand(_BaseCommand):
    @property
    def command_name(self) -> str:
        return "boom"

    @property
    def context_operations(self):
        return [ContextOperation(name="explode", description="raises")]

    def execute_context_operation(self, operation, params):
        raise RuntimeError("upstream on fire")


class _PlainCommand(_BaseCommand):
    """No context capability — the default for every existing command."""

    @property
    def command_name(self) -> str:
        return "plain"


@pytest.fixture
def mqtt_client():
    return MagicMock()


@pytest.fixture
def discovery():
    by_name = {
        "calendar": _CalendarCommand(),
        "boom": _ExplodingCommand(),
        "plain": _PlainCommand(),
    }
    fake = MagicMock()
    fake.get_command.side_effect = lambda n: by_name.get(n)
    fake.get_all_commands.return_value = by_name
    with patch.object(context_handler, "_discovery", return_value=fake):
        yield fake


TOPIC = "jarvis/nodes/node-1/context/query"
OPS_TOPIC = "jarvis/nodes/node-1/context/operations"
CID = "corr-1"


def _request(client: MagicMock, topic: str, op: str, payload: dict[str, Any]) -> None:
    context_handler.handle_context_request(
        client, topic, json.dumps(payload).encode(), op
    )


def _response(client: MagicMock) -> dict[str, Any]:
    assert client.publish.called, "no response published — CC would time out"
    topic, payload = client.publish.call_args.args[0], client.publish.call_args.args[1]
    assert topic.endswith(f"/response/{CID}")
    return json.loads(payload.decode())


# ── Discovery ──────────────────────────────────────────────────────────────


class TestOperationsDiscovery:
    def test_lists_only_commands_declaring_ops(self, mqtt_client, discovery):
        _request(mqtt_client, OPS_TOPIC, "operations", {"correlation_id": CID})
        body = _response(mqtt_client)
        names = {p["command_name"] for p in body["providers"]}
        assert names == {"calendar", "boom"}  # "plain" declares none
        cal = next(p for p in body["providers"] if p["command_name"] == "calendar")
        assert cal["operations"][0]["name"] == "availability"
        assert cal["operations"][0]["params_schema"]["start"]["required"] is True

    def test_no_correlation_id_no_publish(self, mqtt_client, discovery):
        _request(mqtt_client, OPS_TOPIC, "operations", {})
        mqtt_client.publish.assert_not_called()


# ── Query ──────────────────────────────────────────────────────────────────


class TestQuery:
    def test_resolves_provider_without_naming_the_command(self, mqtt_client, discovery):
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {
                "correlation_id": CID,
                "operation": "availability",
                "params": {"start": "2026-07-20", "end": "2026-07-27"},
            },
        )
        body = _response(mqtt_client)
        assert body["ok"] is True
        assert body["command_name"] == "calendar"
        assert body["operation"] == "availability"
        assert body["data"]["free"] == ["Thu 14:00-15:30"]

    def test_explicit_command_honored(self, mqtt_client, discovery):
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {
                "correlation_id": CID,
                "command": "calendar",
                "operation": "availability",
                "params": {"start": "a", "end": "b"},
            },
        )
        assert _response(mqtt_client)["ok"] is True

    def test_unknown_command_reports_not_installed(self, mqtt_client, discovery):
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {
                "correlation_id": CID,
                "command": "nope",
                "operation": "availability",
                "params": {},
            },
        )
        body = _response(mqtt_client)
        assert body["ok"] is False and "not installed" in body["error"]

    def test_no_provider_is_a_typed_code(self, mqtt_client, discovery):
        """CC distinguishes 'nobody provides this' from a transport failure."""
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {"correlation_id": CID, "operation": "inventory", "params": {}},
        )
        body = _response(mqtt_client)
        assert body["ok"] is False
        assert body["code"] == "no_provider"

    def test_missing_required_params_rejected_before_dispatch(
        self, mqtt_client, discovery
    ):
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {
                "correlation_id": CID,
                "operation": "availability",
                "params": {"start": "2026-07-20"},
            },
        )
        body = _response(mqtt_client)
        assert body["ok"] is False and "end" in body["error"]

    def test_provider_exception_becomes_a_response(self, mqtt_client, discovery):
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {"correlation_id": CID, "operation": "explode", "params": {}},
        )
        body = _response(mqtt_client)
        assert body["ok"] is False and "upstream on fire" in body["error"]

    def test_missing_operation_rejected(self, mqtt_client, discovery):
        _request(mqtt_client, TOPIC, "query", {"correlation_id": CID})
        assert _response(mqtt_client)["ok"] is False

    def test_non_dict_params_rejected(self, mqtt_client, discovery):
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {"correlation_id": CID, "operation": "availability", "params": "nope"},
        )
        assert _response(mqtt_client)["ok"] is False


# ── Dispatch hygiene ───────────────────────────────────────────────────────


class TestDispatch:
    def test_unknown_op_is_ignored(self, mqtt_client, discovery):
        _request(mqtt_client, TOPIC, "frobnicate", {"correlation_id": CID})
        mqtt_client.publish.assert_not_called()

    def test_invalid_json_is_ignored(self, mqtt_client, discovery):
        context_handler.handle_context_request(
            mqtt_client, TOPIC, b"{not json", "query"
        )
        mqtt_client.publish.assert_not_called()

    def test_supported_ops_matches_handlers(self):
        assert context_handler.SUPPORTED_OPS == {"operations", "query"}

    def test_oversized_result_is_capped(self, mqtt_client, discovery):
        class _Huge(_BaseCommand):
            @property
            def command_name(self) -> str:
                return "huge"

            @property
            def context_operations(self):
                return [ContextOperation(name="huge", description="big")]

            def execute_context_operation(self, operation, params):
                return ContextResult(data={"blob": "x" * (200 * 1024)})

        discovery.get_all_commands.return_value = {"huge": _Huge()}
        _request(
            mqtt_client,
            TOPIC,
            "query",
            {"correlation_id": CID, "operation": "huge", "params": {}},
        )
        body = _response(mqtt_client)
        assert body["ok"] is False and "too large" in body["error"]
