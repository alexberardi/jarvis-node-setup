"""MQTT handlers for plan-time context queries.

Server-side planners (jarvis-command-center) ask the node's installed
commands for typed, read-only context BEFORE acting — the first consumer is
the phone-call plan-draft step asking the calendar command for
``availability`` so the user's confirm card carries a real constraint
envelope instead of a fill-me-in placeholder.

Subscribed topics (under the node's wildcard `jarvis/nodes/{node_id}/#`):
    .../context/operations  -- list every context op declared on this node
    .../context/query       -- run one op: {command, operation, params}

The op set here is deliberately STATIC (two routable topics) while the
context operations themselves are dynamic — the operation name travels in
the payload, not the topic, so a newly installed command needs no listener
change. Mirrors `command_data_handler`: `correlation_id` in, response on
`{request_topic}/response/{correlation_id}`, subscribe-before-publish on
the CC side (`app/core/mqtt_client.py`).

Contract (phone-calls PRD, cross-agent-context):
- Plan-time only. Never invoked from inside a live call.
- Read-only. Ops must not mutate command state.
- Credentials stay on the node; only the answer travels.
- Every failure path publishes a response — a silent drop would manifest
  as a CC-side timeout, and the planner would degrade for the wrong reason.
"""

from __future__ import annotations

import json
from typing import Any, Optional

import paho.mqtt.client as mqtt

from jarvis_log_client import JarvisLogger

from jarvis_command_sdk import IJarvisCommand

logger = JarvisLogger(service="jarvis-node")

# A context answer crosses MQTT; cap it so a runaway provider can't wedge
# the broker or blow CC's request timeout.
MAX_RESPONSE_BYTES = 128 * 1024


# ── Helpers ────────────────────────────────────────────────────────────────


def _discovery():
    """Command discovery, imported lazily.

    Deliberately not a module-level import: it drags in the node's whole
    repository stack, which makes this module unimportable in contexts that
    don't need it (and trips an existing test-collection import-order
    landmine — `repositories` gets shadowed part-way through the suite).
    """
    from utils.command_discovery_service import get_command_discovery_service

    return get_command_discovery_service()


def _publish(
    client: mqtt.Client,
    request_topic: str,
    correlation_id: str,
    response: dict[str, Any],
) -> None:
    """Publish a response to the per-correlation response topic."""
    topic = f"{request_topic}/response/{correlation_id}"
    try:
        payload = json.dumps(response).encode()
    except (TypeError, ValueError) as exc:
        logger.error(
            "context response serialisation failed",
            topic=topic,
            error=str(exc),
        )
        payload = json.dumps(
            {"ok": False, "error": "context result was not serialisable"}
        ).encode()
    if len(payload) > MAX_RESPONSE_BYTES:
        logger.warning(
            "context response too large",
            topic=topic,
            size=len(payload),
        )
        payload = json.dumps(
            {"ok": False, "error": "context result too large"}
        ).encode()
    client.publish(topic, payload, qos=1)


def _declared_ops(cmd: IJarvisCommand) -> list[Any]:
    """Context ops a command declares; never raises on a bad implementation."""
    try:
        ops = cmd.context_operations
    except Exception as exc:  # noqa: BLE001 — a bad command must not break discovery
        logger.warning(
            "context_operations raised",
            command=getattr(cmd, "command_name", "?"),
            error=str(exc),
        )
        return []
    return list(ops or [])


def _find_operation(cmd: IJarvisCommand, operation: str) -> Optional[Any]:
    for op in _declared_ops(cmd):
        if getattr(op, "name", None) == operation:
            return op
    return None


# ── Ops ────────────────────────────────────────────────────────────────────


def _op_operations(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """Discovery: every context op declared by every installed command."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    discovery = _discovery()
    providers: list[dict[str, Any]] = []
    for cmd in discovery.get_all_commands().values():
        ops = _declared_ops(cmd)
        if not ops:
            continue
        providers.append(
            {
                "command_name": cmd.command_name,
                "operations": [
                    op.to_dict() if hasattr(op, "to_dict") else {"name": str(op)}
                    for op in ops
                ],
            }
        )
    _publish(client, request_topic, correlation_id, {"ok": True, "providers": providers})


def _op_query(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """Run one declared context operation and publish its result.

    ``command`` is optional: when omitted, the first installed command
    declaring the operation answers it, so a planner can ask for
    "availability" without knowing which package provides it.
    """
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    operation = payload.get("operation")
    if not operation or not isinstance(operation, str):
        _publish(
            client,
            request_topic,
            correlation_id,
            {"ok": False, "error": "operation is required"},
        )
        return

    params = payload.get("params") or {}
    if not isinstance(params, dict):
        _publish(
            client,
            request_topic,
            correlation_id,
            {"ok": False, "error": "params must be an object"},
        )
        return

    discovery = _discovery()
    requested_command = payload.get("command")

    cmd: Optional[IJarvisCommand] = None
    op = None
    if requested_command:
        cmd = discovery.get_command(str(requested_command))
        if cmd is None:
            _publish(
                client,
                request_topic,
                correlation_id,
                {"ok": False, "error": f"command '{requested_command}' not installed"},
            )
            return
        op = _find_operation(cmd, operation)
    else:
        for candidate in discovery.get_all_commands().values():
            found = _find_operation(candidate, operation)
            if found is not None:
                cmd, op = candidate, found
                break

    if cmd is None or op is None:
        _publish(
            client,
            request_topic,
            correlation_id,
            {
                "ok": False,
                "error": f"no installed command provides '{operation}'",
                "code": "no_provider",
            },
        )
        return

    missing = op.missing_required(params) if hasattr(op, "missing_required") else []
    if missing:
        _publish(
            client,
            request_topic,
            correlation_id,
            {"ok": False, "error": f"missing required params: {', '.join(missing)}"},
        )
        return

    try:
        result = cmd.execute_context_operation(operation, params)
    except Exception as exc:  # noqa: BLE001 — provider errors are data, not crashes
        logger.error(
            "context operation raised",
            command=cmd.command_name,
            operation=operation,
            error=str(exc),
        )
        _publish(
            client,
            request_topic,
            correlation_id,
            {"ok": False, "error": f"{cmd.command_name}.{operation} failed: {exc}"},
        )
        return

    body = (
        result.to_dict()
        if hasattr(result, "to_dict")
        else {"ok": True, "data": result, "error": None}
    )
    body["command_name"] = cmd.command_name
    body["operation"] = operation
    _publish(client, request_topic, correlation_id, body)


_OP_HANDLERS = {
    "operations": _op_operations,
    "query": _op_query,
}

SUPPORTED_OPS: frozenset[str] = frozenset(_OP_HANDLERS)


def handle_context_request(
    client: mqtt.Client,
    request_topic: str,
    raw_payload: bytes,
    op: str,
) -> None:
    """Entry point invoked by `mqtt_tts_listener.on_message`.

    Every response publish happens inside the op handlers so error paths
    still write to the response topic — a silent failure here shows up as a
    CC-side timeout, which reads as "node offline" rather than "bad request".
    """
    handler = _OP_HANDLERS.get(op)
    if handler is None:
        logger.warning("context unknown op", op=op, topic=request_topic)
        return

    try:
        payload: dict[str, Any] = json.loads(raw_payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("context invalid JSON payload", topic=request_topic, error=str(exc))
        return

    if not isinstance(payload, dict):
        logger.warning(
            "context non-dict payload",
            topic=request_topic,
            type=type(payload).__name__,
        )
        return

    try:
        handler(client, request_topic, payload)
    except Exception as exc:  # noqa: BLE001
        correlation_id = payload.get("correlation_id")
        if correlation_id and isinstance(correlation_id, str):
            _publish(
                client,
                request_topic,
                correlation_id,
                {"ok": False, "error": f"context handler failed: {exc}"},
            )
        logger.error("context handler failed", topic=request_topic, error=str(exc))
