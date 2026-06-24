"""MQTT handlers for the mobile command-data browser.

Subscribed topics (under the node's wildcard `jarvis/nodes/{node_id}/#`):
    .../command-data/commands  -- list visible commands on this node
    .../command-data/schema    -- get a command's FieldSpec list + mode
    .../command-data/list      -- list records for one command
    .../command-data/get       -- get one record by data_key
    .../command-data/create    -- create a new record (opt-in per command)
    .../command-data/update    -- patch one record (dict overlay)
    .../command-data/delete    -- delete one record

Request payload always includes `correlation_id` (UUID). Each response is
published to `{request_topic}/response/{correlation_id}` so that
jarvis-command-center can subscribe before publishing and await the
matching response. This mirrors the subscribe-before-publish pattern
documented in `app/core/mqtt_client.py:subscribe_and_wait`.

Visibility rules (defense-in-depth — CC also filters):
- A command with `data_browser_mode == "disabled"` is hidden entirely.
- Records carrying a `user_id` field are filtered by the caller's
  `requesting_user_id`; rows without `user_id` are treated as legacy and
  visible to everyone (the SDK contract is that the field defaults to None
  until a command starts populating it).
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional

import paho.mqtt.client as mqtt

from jarvis_log_client import JarvisLogger

from jarvis_command_sdk import IJarvisCommand
from db import SessionLocal
from repositories.command_data_repository import CommandDataRepository
from utils.command_discovery_service import get_command_discovery_service

logger = JarvisLogger(service="jarvis-node")

# Cap list responses so a noisy command can't OOM the broker or blow the
# CC-side MQTT timeout. Mobile gets a `truncated: True` flag and can
# refine its filter (or, eventually, paginate).
LIST_CAP = 200


# ── Helpers ────────────────────────────────────────────────────────────────


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
        # Defensive: never let a serialisation bug make CC time out silently.
        logger.error(
            "command-data response serialisation failed",
            topic=topic,
            error=str(exc),
        )
        payload = json.dumps({"ok": False, "error": {"message": "serialisation failed"}}).encode()
    client.publish(topic, payload, qos=1)


def _resolve_by_command_name(name: str) -> Optional[IJarvisCommand]:
    """Resolve an IJarvisCommand by its public `command_name`."""
    discovery = get_command_discovery_service()
    return discovery.get_command(name)


def _visible(cmd: IJarvisCommand) -> bool:
    """A command is visible to the browser iff its mode isn't "disabled".

    Unknown modes pass through unchanged — forward-compat for future modes
    like "readonly" that mobile may render in newer builds. Mobile decides
    rendering."""
    return cmd.data_browser_mode != "disabled"


def _user_can_see(record: dict[str, Any], requesting_user_id: Optional[int]) -> bool:
    if requesting_user_id is None:
        return True
    owner = record.get("user_id")
    return owner is None or owner == requesting_user_id


def _editable_field_names(cmd: IJarvisCommand) -> set[str]:
    """Field names mobile is allowed to patch.

    The data_key (the storage primary key) is always immutable regardless
    of the FieldSpec list and is filtered separately by callers."""
    return {f.name for f in cmd.editable_fields() if f.editable}


def _strip_storage_meta(record: dict[str, Any]) -> dict[str, Any]:
    """`CommandDataRepository.get_all` adds `_data_key` and `_expires_at`
    fields for callers; strip them when echoing back to mobile so the
    record shape matches what the command sees."""
    return {k: v for k, v in record.items() if not k.startswith("_")}


# ── Per-op handlers ────────────────────────────────────────────────────────


def _op_commands(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """List commands on this node that are visible to the browser."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    discovery = get_command_discovery_service()
    commands: list[dict[str, Any]] = []
    for cmd in discovery.get_all_commands().values():
        if not _visible(cmd):
            continue
        commands.append(
            {
                "command_name": cmd.command_name,
                "mode": cmd.data_browser_mode,
                "storage_name": cmd.data_browser_storage_name,
            }
        )
    _publish(client, request_topic, correlation_id, {"commands": commands})


def _op_schema(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """Return one command's FieldSpec list + browser mode."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    command_name = payload.get("command_name")
    if not command_name:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command_name required"},
        })
        return

    cmd = _resolve_by_command_name(command_name)
    if cmd is None or not _visible(cmd):
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command not found"},
        })
        return

    _publish(client, request_topic, correlation_id, {
        "ok": True,
        "mode": cmd.data_browser_mode,
        "supports_create": cmd.data_browser_supports_create,
        "fields": [f.to_dict() for f in cmd.editable_fields()],
    })


def _op_list(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """List records (summaries + raw data) for one command."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    command_name = payload.get("command_name")
    requesting_user_id = payload.get("requesting_user_id")

    cmd = _resolve_by_command_name(command_name) if command_name else None
    if cmd is None or not _visible(cmd):
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command not found"},
        })
        return

    storage_name = cmd.data_browser_storage_name
    db = SessionLocal()
    try:
        repo = CommandDataRepository(db)
        rows = repo.get_all(storage_name)
    finally:
        db.close()

    records: list[dict[str, Any]] = []
    truncated = False
    for raw in rows:
        if len(records) >= LIST_CAP:
            truncated = True
            break
        record = _strip_storage_meta(raw)
        if not _user_can_see(record, requesting_user_id):
            continue
        try:
            summary = cmd.display_summary(record).to_dict()
        except Exception as exc:
            logger.warning(
                "command-data display_summary failed",
                command_name=cmd.command_name,
                data_key=raw.get("_data_key"),
                error=str(exc),
            )
            summary = {"title": str(raw.get("_data_key", "")), "subtitle": None, "icon": "alert-circle-outline"}
        records.append(
            {
                "key": raw.get("_data_key"),
                "summary": summary,
                "data": record,
            }
        )

    _publish(client, request_topic, correlation_id, {
        "ok": True,
        "records": records,
        "truncated": truncated,
        "count": len(records),
    })


def _op_get(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """Get one record's data + the command's schema."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    command_name = payload.get("command_name")
    key = payload.get("key")
    requesting_user_id = payload.get("requesting_user_id")

    cmd = _resolve_by_command_name(command_name) if command_name else None
    if cmd is None or not _visible(cmd) or not key:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command or key not found"},
        })
        return

    db = SessionLocal()
    try:
        repo = CommandDataRepository(db)
        record = repo.get(cmd.data_browser_storage_name, key)
    finally:
        db.close()

    if record is None or not _user_can_see(record, requesting_user_id):
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "record not found"},
        })
        return

    _publish(client, request_topic, correlation_id, {
        "ok": True,
        "record": record,
        "schema": {
            "mode": cmd.data_browser_mode,
            "supports_create": cmd.data_browser_supports_create,
            "fields": [f.to_dict() for f in cmd.editable_fields()],
        },
    })


def _op_update(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """Apply a patch to one record. Dict-overlay semantics.

    The handler filters the incoming patch to fields the command's
    editable_fields() marks editable=True. Anything else is silently
    dropped (defense-in-depth — CC and mobile already filter).

    Commands with runtime state that needs to be re-derived from the
    persisted record (e.g. ReminderService caches the in-memory reminders
    dict and the scheduler reads it) are dispatched to a command-specific
    update path here. Generic commands fall back to a raw repo.save()."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    command_name = payload.get("command_name")
    key = payload.get("key")
    raw_patch = payload.get("patch")
    requesting_user_id = payload.get("requesting_user_id")

    cmd = _resolve_by_command_name(command_name) if command_name else None
    if cmd is None or not _visible(cmd) or not key or not isinstance(raw_patch, dict):
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command, key, or patch missing"},
        })
        return

    if cmd.data_browser_mode == "readonly":
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command is read-only"},
        })
        return

    writable = _editable_field_names(cmd)
    # data_key (the storage primary key) is always immutable, even if the
    # FieldSpec list accidentally marks it editable.
    writable.discard("data_key")
    patch = {k: v for k, v in raw_patch.items() if k in writable}

    if not patch:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "patch has no editable fields"},
        })
        return

    storage_name = cmd.data_browser_storage_name
    updated_record, error = _apply_update(key, patch, requesting_user_id, storage_name)
    if error is not None:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": error},
        })
        return

    logger.info(
        "command-data record updated",
        command_name=cmd.command_name,
        data_key=key,
        patched_keys=sorted(patch.keys()),
        requesting_user_id=requesting_user_id,
    )
    _publish(client, request_topic, correlation_id, {
        "ok": True,
        "record": updated_record,
    })


def _op_create(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """Create a new record for one command.

    Mirrors _op_update but mints a fresh record instead of patching an
    existing one. The record's owner (`user_id`) is stamped server-side from
    `requesting_user_id` inside the command's `data_browser_create` hook — the
    client can never assert ownership, and an unresolved caller is rejected
    fail-closed by the command.

    Create is opt-in per command via `data_browser_supports_create`: a command
    that doesn't opt in is refused here, so a generic save can never silently
    bypass runtime state (e.g. a scheduler that reads an in-memory cache)."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    command_name = payload.get("command_name")
    raw_values = payload.get("data")
    requesting_user_id = payload.get("requesting_user_id")

    cmd = _resolve_by_command_name(command_name) if command_name else None
    if cmd is None or not _visible(cmd) or not isinstance(raw_values, dict):
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command or values missing"},
        })
        return

    if cmd.data_browser_mode == "readonly":
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command is read-only"},
        })
        return

    if not cmd.data_browser_supports_create:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command does not support adding records"},
        })
        return

    # Allowed-on-create = editable fields PLUS create-only fields (e.g. a
    # record's scope, chosen once at birth). Server-managed fields
    # (user_id / id / created_at) are neither editable nor create_only, so a
    # client can't smuggle them in — they're stamped by the command itself.
    creatable = {f.name for f in cmd.editable_fields() if f.editable or f.create_only}
    creatable.discard("data_key")
    values = {k: v for k, v in raw_values.items() if k in creatable}

    try:
        new_key, record = cmd.data_browser_create(values, requesting_user_id)
    except ValueError as exc:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": str(exc)},
        })
        return

    logger.info(
        "command-data record created",
        command_name=cmd.command_name,
        data_key=new_key,
        created_keys=sorted(values.keys()),
        requesting_user_id=requesting_user_id,
    )
    _publish(client, request_topic, correlation_id, {
        "ok": True,
        "record": _strip_storage_meta(record) if isinstance(record, dict) else record,
        "key": new_key,
    })


def _op_delete(
    client: mqtt.Client,
    request_topic: str,
    payload: dict[str, Any],
) -> None:
    """Delete one record."""
    correlation_id = payload.get("correlation_id")
    if not correlation_id:
        return

    command_name = payload.get("command_name")
    key = payload.get("key")
    requesting_user_id = payload.get("requesting_user_id")

    cmd = _resolve_by_command_name(command_name) if command_name else None
    if cmd is None or not _visible(cmd) or not key:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command or key not found"},
        })
        return

    if cmd.data_browser_mode == "readonly":
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "command is read-only"},
        })
        return

    storage_name = cmd.data_browser_storage_name
    db = SessionLocal()
    try:
        repo = CommandDataRepository(db)
        existing = repo.get(storage_name, key)
        if existing is None or not _user_can_see(existing, requesting_user_id):
            _publish(client, request_topic, correlation_id, {
                "ok": False, "error": {"message": "record not found"},
            })
            return
        deleted = _delete_record(key, storage_name, repo)
    finally:
        db.close()

    if not deleted:
        _publish(client, request_topic, correlation_id, {
            "ok": False, "error": {"message": "delete failed"},
        })
        return

    logger.info(
        "command-data record deleted",
        command_name=cmd.command_name,
        data_key=key,
        requesting_user_id=requesting_user_id,
    )
    _publish(client, request_topic, correlation_id, {"ok": True})


# ── Command-specific update / delete paths ────────────────────────────────


def _apply_update(
    key: str,
    patch: dict[str, Any],
    requesting_user_id: Optional[int],
    storage_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Dispatch to a command-specific update path when one exists.

    Falls back to dict-overlay + repo.save() for commands without a
    custom update path. The fallback does NOT touch runtime caches; any
    new command that stores derived state should declare its own path
    here before turning on the browser."""
    if storage_name == "set_reminder":
        # ReminderService holds in-memory state the scheduler reads, so
        # bypass the repo and go through the service.
        from services.reminder_service import get_reminder_service
        service = get_reminder_service()
        reminder, error = service.update_reminder(key, patch, user_id=requesting_user_id)
        if reminder is None:
            return None, error
        return reminder.to_dict(), None

    # Generic fallback. Read, merge, write.
    db = SessionLocal()
    try:
        repo = CommandDataRepository(db)
        existing = repo.get(storage_name, key)
        if existing is None or not _user_can_see(existing, requesting_user_id):
            return None, "record not found"
        merged = {**existing, **patch}
        repo.save(storage_name, key, merged)
        return merged, None
    finally:
        db.close()


def _delete_record(
    key: str,
    storage_name: str,
    repo: CommandDataRepository,
) -> bool:
    """Dispatch to a command-specific delete path when one exists.

    The reminder service's `delete_reminder` evicts the in-memory entry
    and calls the underlying repo; for commands without runtime state we
    just use the repo directly."""
    if storage_name == "set_reminder":
        from services.reminder_service import get_reminder_service
        return get_reminder_service().delete_reminder(key)
    return repo.delete(storage_name, key)


# ── Dispatcher ─────────────────────────────────────────────────────────────


_OP_HANDLERS: dict[str, Callable[[mqtt.Client, str, dict[str, Any]], None]] = {
    "commands": _op_commands,
    "schema": _op_schema,
    "list": _op_list,
    "get": _op_get,
    "create": _op_create,
    "update": _op_update,
    "delete": _op_delete,
}

# The ops the MQTT listener should route here. This is the SINGLE source of
# truth — the listener derives its allowlist from this set, so adding a handler
# to _OP_HANDLERS automatically makes it routable (no second list to keep in
# sync). The historical bug was a hardcoded tuple in the listener that didn't
# include "create", so create requests fell through and timed out CC.
SUPPORTED_OPS: frozenset[str] = frozenset(_OP_HANDLERS)


def handle_command_data_request(
    client: mqtt.Client,
    request_topic: str,
    raw_payload: bytes,
    op: str,
) -> None:
    """Entry point invoked by `mqtt_tts_listener.on_message`.

    Parses JSON, looks up the op handler, runs it. All response publishing
    happens inside the handler so error paths still write to the response
    topic — silent failure here would manifest as a CC-side timeout."""
    handler = _OP_HANDLERS.get(op)
    if handler is None:
        logger.warning("command-data unknown op", op=op, topic=request_topic)
        return

    try:
        payload: dict[str, Any] = json.loads(raw_payload.decode())
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(
            "command-data invalid JSON payload",
            topic=request_topic,
            error=str(exc),
        )
        return

    if not isinstance(payload, dict):
        logger.warning(
            "command-data non-dict payload",
            topic=request_topic,
            type=type(payload).__name__,
        )
        return

    try:
        handler(client, request_topic, payload)
    except Exception as exc:
        # Try to surface the error to CC instead of letting it time out.
        correlation_id = payload.get("correlation_id")
        if correlation_id and isinstance(correlation_id, str):
            _publish(client, request_topic, correlation_id, {
                "ok": False, "error": {"message": f"handler error: {exc}"},
            })
        logger.error(
            "command-data handler crashed",
            op=op,
            topic=request_topic,
            error=str(exc),
        )
