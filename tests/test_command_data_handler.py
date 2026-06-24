"""Tests for command_data_handler — the MQTT-side dispatch for the mobile
command-data browser."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from jarvis_command_sdk import (
    CommandExample,
    CommandResponse,
    DataBrowserMode,
    FieldSpec,
    IJarvisCommand,
    RecordSummary,
)

from services import command_data_handler


# ── Test fixtures ──────────────────────────────────────────────────────────


class _ToyCommand(IJarvisCommand):
    """A minimal command with structured records for testing."""

    storage_name = "toy_storage"

    @property
    def command_name(self) -> str:
        return "toy"

    @property
    def data_browser_storage_name(self) -> str:
        return self.storage_name

    @property
    def description(self) -> str:
        return "Toy command for tests"

    @property
    def parameters(self):
        return []

    @property
    def required_secrets(self):
        return []

    @property
    def keywords(self):
        return ["toy"]

    def generate_prompt_examples(self):
        return [CommandExample("toy", {}, is_primary=True)]

    def generate_adapter_examples(self):
        return self.generate_prompt_examples()

    def editable_fields(self):
        return [
            FieldSpec("id", "id", editable=False),
            FieldSpec("text", "string", editable=True),
            FieldSpec("user_id", "user_ref", editable=False),
        ]

    def display_summary(self, record):
        return RecordSummary(
            title=record.get("text", "?"),
            subtitle=record.get("id"),
            icon="tag",
        )

    def run(self, request_info, **kwargs):
        return CommandResponse.success_response({})


class _DisabledCommand(_ToyCommand):
    @property
    def command_name(self) -> str:
        return "secret"

    @property
    def data_browser_storage_name(self) -> str:
        return "secret_storage"

    @property
    def data_browser_mode(self) -> DataBrowserMode:
        return "disabled"


class _ReadonlyCommand(_ToyCommand):
    @property
    def command_name(self) -> str:
        return "ro"

    @property
    def data_browser_storage_name(self) -> str:
        return "ro_storage"

    @property
    def data_browser_mode(self) -> DataBrowserMode:
        return "readonly"


class _CreatableCommand(_ToyCommand):
    """Opts into create with a custom hook so the node delegates to it.

    Records the (filtered) fields + user it received so tests can assert the
    node stamps ownership server-side and drops non-creatable fields."""

    def __init__(self) -> None:
        super().__init__()
        self.last_create: dict[str, Any] | None = None

    @property
    def command_name(self) -> str:
        return "creatable"

    @property
    def data_browser_storage_name(self) -> str:
        return "creatable_storage"

    @property
    def data_browser_supports_create(self) -> bool:
        return True

    def editable_fields(self):
        return [
            FieldSpec("id", "id", editable=False),
            FieldSpec("text", "string", editable=True),
            FieldSpec(
                "scope", "enum", enum_values=["personal", "household"],
                editable=False, create_only=True,
            ),
            FieldSpec("user_id", "user_ref", editable=False),
        ]

    def data_browser_create(self, fields, requesting_user_id):
        self.last_create = {"fields": dict(fields), "user": requesting_user_id}
        if requesting_user_id is None and fields.get("scope", "personal") == "personal":
            raise ValueError("cannot create a personal record without a known user")
        key = "new-key"
        return key, {
            "id": key,
            "text": fields.get("text"),
            "scope": fields.get("scope"),
            "user_id": requesting_user_id,
        }


@pytest.fixture
def mqtt_client():
    return MagicMock()


@pytest.fixture
def discovery():
    """Patch get_command_discovery_service so tests register their own
    command set without hitting the real on-disk discovery."""
    toy = _ToyCommand()
    disabled = _DisabledCommand()
    readonly = _ReadonlyCommand()
    creatable = _CreatableCommand()

    fake_discovery = MagicMock()
    by_name = {
        "toy": toy,
        "secret": disabled,
        "ro": readonly,
        "creatable": creatable,
    }
    fake_discovery.get_command.side_effect = lambda n: by_name.get(n)
    fake_discovery.get_all_commands.return_value = by_name

    with patch.object(
        command_data_handler, "get_command_discovery_service", return_value=fake_discovery
    ):
        yield fake_discovery


@pytest.fixture
def repo(discovery):
    """Patch CommandDataRepository so tests don't need a real DB session."""
    rows_by_storage: dict[str, list[dict[str, Any]]] = {
        "toy_storage": [
            {"_data_key": "row-1", "id": "row-1", "text": "alpha", "user_id": 42},
            {"_data_key": "row-2", "id": "row-2", "text": "beta", "user_id": 99},
            {"_data_key": "row-3", "id": "row-3", "text": "legacy"},  # no user_id
        ],
    }

    fake_repo = MagicMock()

    def get_all(storage_name, include_expired=False):
        return [dict(r) for r in rows_by_storage.get(storage_name, [])]

    def get(storage_name, key, include_expired=False):
        for r in rows_by_storage.get(storage_name, []):
            if r["_data_key"] == key:
                # repo.get strips storage meta itself; the test fixture
                # returns the same shape the real one returns (no _data_key).
                return {k: v for k, v in r.items() if not k.startswith("_")}
        return None

    def save(storage_name, key, data):
        rows = rows_by_storage.setdefault(storage_name, [])
        for r in rows:
            if r["_data_key"] == key:
                r.clear()
                r["_data_key"] = key
                r.update(data)
                return r
        rows.append({"_data_key": key, **data})

    def delete(storage_name, key):
        rows = rows_by_storage.get(storage_name, [])
        for i, r in enumerate(rows):
            if r["_data_key"] == key:
                rows.pop(i)
                return True
        return False

    fake_repo.get_all.side_effect = get_all
    fake_repo.get.side_effect = get
    fake_repo.save.side_effect = save
    fake_repo.delete.side_effect = delete

    fake_session = MagicMock()
    with patch.object(command_data_handler, "SessionLocal", return_value=fake_session), \
         patch.object(command_data_handler, "CommandDataRepository", return_value=fake_repo):
        yield fake_repo


def _publish_capture(mqtt_client) -> dict[str, Any]:
    """Return the most recent publish payload, parsed."""
    mqtt_client.publish.assert_called()
    args, _ = mqtt_client.publish.call_args
    topic, payload = args[0], args[1]
    return {"topic": topic, "body": json.loads(payload.decode())}


def _request(mqtt_client, op: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Invoke handle_command_data_request and return the publish capture."""
    request_topic = f"jarvis/nodes/test/command-data/{op}"
    command_data_handler.handle_command_data_request(
        mqtt_client, request_topic, json.dumps(payload).encode(), op
    )
    return _publish_capture(mqtt_client)


# ── commands op ────────────────────────────────────────────────────────────


class TestCommands:
    def test_lists_visible_commands(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "commands", {"correlation_id": "c1"})
        assert result["topic"].endswith("/commands/response/c1")
        names = {c["command_name"] for c in result["body"]["commands"]}
        # secret is disabled, so excluded; toy + ro stay
        assert "toy" in names
        assert "ro" in names
        assert "secret" not in names

    def test_no_correlation_id_no_publish(self, mqtt_client, discovery, repo) -> None:
        request_topic = "jarvis/nodes/test/command-data/commands"
        command_data_handler.handle_command_data_request(
            mqtt_client, request_topic, b"{}", "commands"
        )
        mqtt_client.publish.assert_not_called()

    def test_response_carries_storage_name(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "commands", {"correlation_id": "c1"})
        toy = next(c for c in result["body"]["commands"] if c["command_name"] == "toy")
        assert toy["storage_name"] == "toy_storage"


# ── schema op ──────────────────────────────────────────────────────────────


class TestSchema:
    def test_returns_fields_and_mode(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "schema", {
            "correlation_id": "c1", "command_name": "toy"
        })
        body = result["body"]
        assert body["ok"] is True
        assert body["mode"] == "enabled"
        assert body["supports_create"] is False  # toy doesn't opt in
        field_names = [f["name"] for f in body["fields"]]
        assert field_names == ["id", "text", "user_id"]

    def test_reports_supports_create(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "schema", {
            "correlation_id": "c1", "command_name": "creatable"
        })
        body = result["body"]
        assert body["ok"] is True
        assert body["supports_create"] is True
        scope = next(f for f in body["fields"] if f["name"] == "scope")
        assert scope["create_only"] is True  # round-trips through to_dict

    def test_disabled_command_returns_not_found(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "schema", {
            "correlation_id": "c1", "command_name": "secret"
        })
        body = result["body"]
        assert body["ok"] is False
        assert "not found" in body["error"]["message"]

    def test_readonly_command_visible(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "schema", {
            "correlation_id": "c1", "command_name": "ro"
        })
        body = result["body"]
        assert body["ok"] is True
        assert body["mode"] == "readonly"

    def test_missing_command_name(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "schema", {"correlation_id": "c1"})
        body = result["body"]
        assert body["ok"] is False


# ── list op ────────────────────────────────────────────────────────────────


class TestList:
    def test_lists_all_records_no_user_filter(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "list", {
            "correlation_id": "c1", "command_name": "toy"
        })
        body = result["body"]
        assert body["ok"] is True
        assert body["count"] == 3
        # First record's summary uses display_summary
        first = body["records"][0]
        assert first["summary"]["title"] == "alpha"
        assert first["summary"]["icon"] == "tag"
        assert first["key"] == "row-1"

    def test_filters_by_requesting_user(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "list", {
            "correlation_id": "c1",
            "command_name": "toy",
            "requesting_user_id": 42,
        })
        body = result["body"]
        # User 42 sees row-1 (theirs) and row-3 (legacy null user_id), not row-2
        keys = [r["key"] for r in body["records"]]
        assert "row-1" in keys
        assert "row-3" in keys
        assert "row-2" not in keys

    def test_disabled_command_is_404(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "list", {
            "correlation_id": "c1", "command_name": "secret"
        })
        assert result["body"]["ok"] is False

    def test_unknown_command_is_404(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "list", {
            "correlation_id": "c1", "command_name": "nope"
        })
        assert result["body"]["ok"] is False

    def test_truncation_cap(self, mqtt_client, discovery, repo, monkeypatch) -> None:
        # Inflate the toy storage past LIST_CAP
        monkeypatch.setattr(command_data_handler, "LIST_CAP", 2)
        result = _request(mqtt_client, "list", {
            "correlation_id": "c1", "command_name": "toy"
        })
        body = result["body"]
        assert body["count"] == 2
        assert body["truncated"] is True


# ── get op ─────────────────────────────────────────────────────────────────


class TestGet:
    def test_returns_record_and_schema(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "get", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-1",
        })
        body = result["body"]
        assert body["ok"] is True
        assert body["record"]["text"] == "alpha"
        assert body["schema"]["mode"] == "enabled"
        assert len(body["schema"]["fields"]) == 3

    def test_cross_user_get_is_404(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "get", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-2",  # owned by user 99
            "requesting_user_id": 42,
        })
        assert result["body"]["ok"] is False

    def test_unknown_key(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "get", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "no-such",
        })
        assert result["body"]["ok"] is False


# ── update op ──────────────────────────────────────────────────────────────


class TestCreate:
    def _create(self, mqtt_client, **payload):
        payload.setdefault("correlation_id", "c1")
        payload.setdefault("command_name", "creatable")
        return _request(mqtt_client, "create", payload)["body"]

    def test_creates_and_echoes_key_and_owner(self, mqtt_client, discovery, repo) -> None:
        body = self._create(
            mqtt_client,
            data={"text": "hello", "scope": "personal"},
            requesting_user_id=42,
        )
        assert body["ok"] is True
        assert body["key"] == "new-key"
        assert body["record"]["user_id"] == 42  # stamped from requesting_user_id
        assert body["record"]["scope"] == "personal"
        assert body["record"]["text"] == "hello"

    def test_drops_non_creatable_fields_before_hook(self, mqtt_client, discovery, repo) -> None:
        # Client tries to smuggle id / user_id / an unknown field; the node
        # filters to editable + create_only names before the command sees them.
        self._create(
            mqtt_client,
            data={"text": "x", "scope": "household", "user_id": 999, "id": "evil", "bogus": 1},
            requesting_user_id=7,
        )
        seen = discovery.get_command("creatable").last_create["fields"]
        assert seen == {"text": "x", "scope": "household"}

    def test_unsupported_command_is_refused(self, mqtt_client, discovery, repo) -> None:
        # toy doesn't opt into create
        body = self._create(
            mqtt_client, command_name="toy", data={"text": "x"}, requesting_user_id=1
        )
        assert body["ok"] is False
        assert "support" in body["error"]["message"].lower()

    def test_readonly_command_rejected(self, mqtt_client, discovery, repo) -> None:
        body = self._create(
            mqtt_client, command_name="ro", data={"text": "x"}, requesting_user_id=1
        )
        assert body["ok"] is False
        assert "read-only" in body["error"]["message"].lower()

    def test_disabled_command_is_not_found(self, mqtt_client, discovery, repo) -> None:
        body = self._create(
            mqtt_client, command_name="secret", data={"text": "x"}, requesting_user_id=1
        )
        assert body["ok"] is False

    def test_missing_data_is_error(self, mqtt_client, discovery, repo) -> None:
        body = self._create(mqtt_client, requesting_user_id=1)  # no data dict
        assert body["ok"] is False

    def test_hook_value_error_surfaces_message(self, mqtt_client, discovery, repo) -> None:
        # personal scope with an unresolved user -> hook fails closed
        body = self._create(
            mqtt_client, data={"text": "x", "scope": "personal"}, requesting_user_id=None
        )
        assert body["ok"] is False
        assert "without a known user" in body["error"]["message"]

    def test_no_correlation_id_no_publish(self, mqtt_client, discovery, repo) -> None:
        request_topic = "jarvis/nodes/test/command-data/create"
        command_data_handler.handle_command_data_request(
            mqtt_client,
            request_topic,
            json.dumps({"command_name": "creatable", "data": {"text": "x"}, "requesting_user_id": 1}).encode(),
            "create",
        )
        mqtt_client.publish.assert_not_called()


class TestUpdate:
    def test_basic_field_update(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "update", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-1",
            "patch": {"text": "renamed"},
            "requesting_user_id": 42,
        })
        body = result["body"]
        assert body["ok"] is True
        assert body["record"]["text"] == "renamed"

    def test_non_editable_field_silently_dropped(self, mqtt_client, discovery, repo) -> None:
        # user_id is marked editable=False
        result = _request(mqtt_client, "update", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-1",
            "patch": {"user_id": 99, "text": "kept"},
            "requesting_user_id": 42,
        })
        body = result["body"]
        # Only "text" survives the writable-name filter; user_id stays 42.
        assert body["ok"] is True
        assert body["record"]["text"] == "kept"
        assert body["record"]["user_id"] == 42

    def test_patch_with_zero_editable_fields_is_error(
        self, mqtt_client, discovery, repo
    ) -> None:
        # All keys filtered out → no-op patch is treated as a client error.
        result = _request(mqtt_client, "update", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-1",
            "patch": {"id": "evil", "user_id": 99},
        })
        body = result["body"]
        assert body["ok"] is False

    def test_readonly_command_rejected(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "update", {
            "correlation_id": "c1",
            "command_name": "ro",
            "key": "row-1",
            "patch": {"text": "x"},
        })
        body = result["body"]
        assert body["ok"] is False
        assert "read-only" in body["error"]["message"]

    def test_cross_user_update_is_404(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "update", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-2",  # belongs to user 99
            "patch": {"text": "stolen"},
            "requesting_user_id": 42,
        })
        assert result["body"]["ok"] is False


# ── delete op ──────────────────────────────────────────────────────────────


class TestDelete:
    def test_deletes_owned_record(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "delete", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-1",
            "requesting_user_id": 42,
        })
        assert result["body"]["ok"] is True

    def test_cross_user_delete_is_404(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "delete", {
            "correlation_id": "c1",
            "command_name": "toy",
            "key": "row-2",
            "requesting_user_id": 42,
        })
        assert result["body"]["ok"] is False

    def test_readonly_command_rejected(self, mqtt_client, discovery, repo) -> None:
        result = _request(mqtt_client, "delete", {
            "correlation_id": "c1",
            "command_name": "ro",
            "key": "row-1",
        })
        body = result["body"]
        assert body["ok"] is False
        assert "read-only" in body["error"]["message"]


# ── Malformed input ────────────────────────────────────────────────────────


class TestMalformedInput:
    def test_invalid_json_no_publish(self, mqtt_client, discovery, repo) -> None:
        command_data_handler.handle_command_data_request(
            mqtt_client, "jarvis/nodes/test/command-data/list", b"\xff\xfe garbage", "list"
        )
        mqtt_client.publish.assert_not_called()

    def test_non_dict_payload_no_publish(self, mqtt_client, discovery, repo) -> None:
        command_data_handler.handle_command_data_request(
            mqtt_client, "jarvis/nodes/test/command-data/list", b"[1,2,3]", "list"
        )
        mqtt_client.publish.assert_not_called()

    def test_unknown_op_no_publish(self, mqtt_client, discovery, repo) -> None:
        command_data_handler.handle_command_data_request(
            mqtt_client,
            "jarvis/nodes/test/command-data/unknown",
            json.dumps({"correlation_id": "c1"}).encode(),
            "unknown",
        )
        mqtt_client.publish.assert_not_called()
