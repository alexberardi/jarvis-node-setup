"""Tests for ExportShoppingListCommand — inbox export + export_selected callback."""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

import commands.export_shopping_list_command as eslc
from commands.export_shopping_list_command import (
    ExportShoppingListCommand,
    _build_notes_result,
    _build_walmart_result,
    PROVIDERS,
)
from commands.shopping_list_command import _item_key
from core.request_information import RequestInformation


_DEFAULT_USER_ID = 42


class FakeStorage:
    """In-memory stand-in for JarvisStorage.

    Mirrors CommandDataRepository.get_all exactly: meta fields are
    underscore-prefixed (`_data_key`, `_expires_at`) — the real backend
    does NOT inject a bare `data_key`.
    """

    def __init__(self) -> None:
        self._data: Dict[str, Dict[str, Any]] = {}
        self._secrets: Dict[str, str] = {}

    def save(self, key: str, data: Dict[str, Any], expires_at=None) -> None:
        self._data[key] = dict(data)

    def get(self, key: str) -> Dict[str, Any] | None:
        record = self._data.get(key)
        return dict(record) if record is not None else None

    def get_all(self) -> List[Dict[str, Any]]:
        return [
            {**record, "_data_key": key, "_expires_at": None}
            for key, record in self._data.items()
        ]

    def delete(self, key: str) -> bool:
        return self._data.pop(key, None) is not None

    def delete_all(self) -> int:
        count = len(self._data)
        self._data.clear()
        return count

    def get_secret(self, key: str, scope: str | None = None) -> str | None:
        return self._secrets.get(key)


@pytest.fixture
def shopping_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def map_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def command(shopping_storage: FakeStorage, map_storage: FakeStorage):
    # logger is mocked so failure-path tests don't leave the log client's
    # background flush thread writing to pytest's closed capture streams.
    with patch.object(eslc, "_get_shopping_storage", return_value=shopping_storage), \
         patch.object(eslc, "_get_map_storage", return_value=map_storage), \
         patch.object(eslc, "logger", MagicMock()):
        yield ExportShoppingListCommand()


@pytest.fixture
def post_mock():
    with patch.object(
        ExportShoppingListCommand, "_post_inbox_item", return_value="ok"
    ) as mock:
        yield mock


@pytest.fixture
def request_info() -> RequestInformation:
    return RequestInformation(
        voice_command="send my shopping list to walmart",
        conversation_id="conv-1",
        user_id=_DEFAULT_USER_ID,
    )


@pytest.fixture
def request_info_unknown_speaker() -> RequestInformation:
    return RequestInformation(
        voice_command="send my shopping list to walmart",
        conversation_id="conv-1",
        user_id=None,
    )


def _add_list_item(
    storage: FakeStorage,
    name: str,
    *,
    checked: bool = False,
    regular: bool = False,
    checked_at: str | None = None,
) -> str:
    key = _item_key(name)
    storage.save(key, {
        "item": name,
        "checked": checked,
        "regular": regular,
        "added_at": "2026-06-09T12:00:00+00:00",
        "checked_at": checked_at,
    })
    return key


def _add_map_entry(storage: FakeStorage, name: str, walmart_item_id: str) -> str:
    key = _item_key(name)
    storage.save(key, {"item": name, "walmart_item_id": walmart_item_id})
    return key


class TestProperties:
    def test_command_name(self, command: ExportShoppingListCommand) -> None:
        assert command.command_name == "export_shopping_list"

    def test_data_browser_storage_name_is_walmart_items(
        self, command: ExportShoppingListCommand
    ) -> None:
        assert command.data_browser_storage_name == "walmart_items"

    def test_no_llm_parameters(self, command: ExportShoppingListCommand) -> None:
        """The destination is configuration + spoken override — never an
        LLM-extracted parameter (models fill optional enums spuriously)."""
        assert command.parameters == []

    def test_keywords(self, command: ExportShoppingListCommand) -> None:
        assert "walmart" in command.keywords
        assert "shopping list" in command.keywords

    def test_export_provider_secret(self, command: ExportShoppingListCommand) -> None:
        secret = next(s for s in command.required_secrets if s.key == "EXPORT_PROVIDER")
        assert secret.required is False
        assert secret.is_sensitive is False
        assert secret.scope == "integration"
        assert secret.enum_values == ["notes", "walmart"]
        assert secret.friendly_name == "Export store"

    def test_antipattern_points_to_shopping_list(
        self, command: ExportShoppingListCommand
    ) -> None:
        assert any(ap.command_name == "shopping_list" for ap in command.antipatterns)

    def test_exactly_one_primary_prompt_example(
        self, command: ExportShoppingListCommand
    ) -> None:
        primaries = [e for e in command.generate_prompt_examples() if e.is_primary]
        assert len(primaries) == 1

    def test_adapter_examples_count(self, command: ExportShoppingListCommand) -> None:
        assert 10 <= len(command.generate_adapter_examples()) <= 25

    def test_callback_registered(self, command: ExportShoppingListCommand) -> None:
        assert "export_selected" in command.get_callbacks()


class TestPostProcess:
    """The destination comes from the utterance, never from the LLM. Any
    LLM-invented provider arg is discarded unconditionally; the spoken
    destination is re-derived from the raw voice_command."""

    def test_llm_provider_discarded_bare_phrasing(
        self, command: ExportShoppingListCommand
    ) -> None:
        args = command.post_process_tool_call(
            {"provider": "walmart"}, "Export my shopping list"
        )
        assert "provider" not in args

    def test_llm_provider_discarded_even_when_valid(
        self, command: ExportShoppingListCommand
    ) -> None:
        """The LLM said notes but the user said walmart — the utterance wins."""
        args = command.post_process_tool_call(
            {"provider": "notes"}, "send my shopping list to walmart"
        )
        assert args == {"provider": "walmart"}

    def test_spoken_notes_detected(
        self, command: ExportShoppingListCommand
    ) -> None:
        args = command.post_process_tool_call({}, "send my shopping list to my notes")
        assert args == {"provider": "notes"}

    def test_spoken_walmart_detected(
        self, command: ExportShoppingListCommand
    ) -> None:
        args = command.post_process_tool_call({}, "send my shopping list to walmart")
        assert args == {"provider": "walmart"}

    def test_store_phrasing_maps_to_walmart(
        self, command: ExportShoppingListCommand
    ) -> None:
        args = command.post_process_tool_call({}, "send the grocery list to the store")
        assert args == {"provider": "walmart"}

    def test_bare_phrasing_defers_to_secret(
        self, command: ExportShoppingListCommand
    ) -> None:
        assert command.post_process_tool_call({}, "export the shopping list") == {}


class TestDataBrowser:
    def test_editable_fields(self, command: ExportShoppingListCommand) -> None:
        fields = {f.name: f for f in command.editable_fields()}
        assert not fields["item"].editable
        assert fields["item"].required
        assert fields["walmart_item_id"].editable

    def test_display_summary_mapped(self, command: ExportShoppingListCommand) -> None:
        summary = command.display_summary({"item": "milk", "walmart_item_id": "12345"})
        assert summary.title == "milk"
        assert summary.subtitle == "Walmart ID: 12345"
        assert summary.icon == "cart-arrow-right"

    def test_display_summary_unmapped(self, command: ExportShoppingListCommand) -> None:
        summary = command.display_summary({"item": "milk", "walmart_item_id": ""})
        assert summary.subtitle == "Needs Walmart ID"

    def test_display_summary_tolerates_empty_record(
        self, command: ExportShoppingListCommand
    ) -> None:
        summary = command.display_summary({})
        assert summary.title
        assert summary.subtitle == "Needs Walmart ID"


class TestSpeakerGate:
    def test_unknown_speaker_refused(
        self,
        command: ExportShoppingListCommand,
        post_mock,
        request_info_unknown_speaker: RequestInformation,
    ) -> None:
        response = command.run(request_info_unknown_speaker)
        assert not response.success
        assert "not sure who's asking" in response.context_data["message"]
        assert "training your voice" in response.context_data["message"]
        post_mock.assert_not_called()


class TestEmptyList:
    def test_empty_list_nothing_to_export(
        self,
        command: ExportShoppingListCommand,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info)
        assert response.success
        assert "empty" in response.context_data["message"]
        assert "nothing to export" in response.context_data["message"]
        post_mock.assert_not_called()

    def test_all_checked_means_empty(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk", checked=True)
        response = command.run(request_info)
        assert response.success
        assert "nothing to export" in response.context_data["message"]
        post_mock.assert_not_called()


def _all_rows(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [row for section in metadata["sections"] for row in section["rows"]]


class TestPayloadShape:
    """The metadata is the interactive_list v1 wire payload — see the
    shared contract (SDK CLAUDE.md, "Interactive List payloads (v1)")."""

    def test_sections_split_regulars_and_one_offs(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk", regular=True)
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "milk", "10450114")

        response = command.run(request_info)
        assert response.success
        post_mock.assert_called_once()
        payload = post_mock.call_args.args[0]

        metadata = payload["metadata"]
        assert metadata["type"] == "interactive_list"
        assert metadata["version"] == 1
        assert metadata["command_name"] == "export_shopping_list"
        assert [s["title"] for s in metadata["sections"]] == ["Regulars", "One-offs"]
        assert [r["key"] for r in metadata["sections"][0]["rows"]] == ["milk"]
        assert [r["key"] for r in metadata["sections"][1]["rows"]] == ["candles"]

    def test_empty_sections_omitted(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Only non-empty sections ship — a one-off-only list has no Regulars."""
        _add_list_item(shopping_storage, "candles")

        command.run(request_info)
        metadata = post_mock.call_args.args[0]["metadata"]
        assert [s["title"] for s in metadata["sections"]] == ["One-offs"]

    def test_walmart_row_shape(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Walmart rows: stepper control, record gate on walmart_item_id,
        Find ID / View webview_pick actions — the exact wire dict."""
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "10450114")

        command.run(request_info)
        metadata = post_mock.call_args.args[0]["metadata"]
        (row,) = _all_rows(metadata)
        assert row == {
            "key": "milk",
            "label": "milk",
            "control": "checkbox_stepper",
            "default": {"selected": True, "quantity": 1},
            "disabled_caption": "No Walmart match",
            "requires_record_field": {
                "command_name": "export_shopping_list",
                "field": "walmart_item_id",
                "field_label": "ID",
            },
            "row_actions": [
                {
                    "label": "Find ID",
                    "type": "webview_pick",
                    "start_url": "https://www.walmart.com/search?q={label}",
                    "pattern": r"/ip/(?:[^/]+/)?(\d{5,})",
                    "save": {
                        "command_name": "export_shopping_list",
                        "field": "walmart_item_id",
                    },
                },
                {
                    "label": "View",
                    "type": "webview_pick",
                    "start_url": "https://www.walmart.com/ip/{value}",
                    "pattern": r"/ip/(?:[^/]+/)?(\d{5,})",
                    "save": {
                        "command_name": "export_shopping_list",
                        "field": "walmart_item_id",
                    },
                },
            ],
        }

    def test_notes_row_shape(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Notes rows: same control/defaults, but no gate and no row actions
        — notes exports need no Walmart IDs."""
        _add_list_item(shopping_storage, "milk")

        command.run(request_info, provider="notes")
        metadata = post_mock.call_args.args[0]["metadata"]
        (row,) = _all_rows(metadata)
        assert row == {
            "key": "milk",
            "label": "milk",
            "control": "checkbox_stepper",
            "default": {"selected": True, "quantity": 1},
        }

    def test_actions_context_and_empty_text(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")

        command.run(request_info)
        metadata = post_mock.call_args.args[0]["metadata"]
        assert metadata["actions"] == [
            {
                "label": "Export {n} items",
                "callback": "export_selected",
                "style": "primary",
            }
        ]
        assert metadata["context"] == {"provider": "walmart"}
        assert metadata["empty_text"] == "Nothing to export"

    def test_payload_envelope(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "eggs")

        command.run(request_info)
        payload = post_mock.call_args.args[0]

        # Push dedup is md5(title+body) over 60s — the count in the title
        # keeps back-to-back exports of different sizes distinct.
        assert "2 items" in payload["title"]
        assert payload["category"] == "interactive_list"
        assert payload["user_id"] == _DEFAULT_USER_ID
        assert payload["create_push_notification"] is True
        assert payload["target_type"] == "user"
        assert payload["summary"]
        # Markdown fallback body lists every item
        assert "- milk" in payload["body"]
        assert "- eggs" in payload["body"]
        # node_id is injected by CC, never sent by the node
        assert "node_id" not in payload["metadata"]

    def test_checked_items_excluded(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "eggs", checked=True)

        command.run(request_info)
        payload = post_mock.call_args.args[0]
        assert [r["label"] for r in _all_rows(payload["metadata"])] == ["milk"]

    def test_filter_is_on_checked_not_checked_at(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Re-added items keep a stale checked_at — they must still export."""
        _add_list_item(
            shopping_storage, "milk",
            checked=False, checked_at="2026-06-01T00:00:00+00:00",
        )
        response = command.run(request_info)
        assert response.success
        payload = post_mock.call_args.args[0]
        assert len(_all_rows(payload["metadata"])) == 1

    def test_empty_string_map_id_counts_as_unmatched(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Pre-seeded-but-unfilled map rows ('') are unmatched. The payload
        no longer carries IDs — gating is live via requires_record_field —
        but the voice message must still count them as needing a match."""
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "candles", "")

        response = command.run(request_info)
        assert "1 still needs a Walmart match" in response.context_data["message"]

    def test_notes_provider_in_context(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")

        response = command.run(request_info, provider="notes")
        assert response.success
        payload = post_mock.call_args.args[0]
        assert payload["metadata"]["context"] == {"provider": "notes"}

    def test_invalid_provider_kwarg_falls_back_to_configured(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")

        command.run(request_info, provider="amazon")
        payload = post_mock.call_args.args[0]
        assert payload["metadata"]["context"] == {"provider": "walmart"}

    def test_provider_secret_drives_context(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        map_storage._secrets["EXPORT_PROVIDER"] = "notes"
        _add_list_item(shopping_storage, "milk")

        command.run(request_info)
        payload = post_mock.call_args.args[0]
        assert payload["metadata"]["context"] == {"provider": "notes"}

    def test_explicit_provider_kwarg_overrides_secret(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        map_storage._secrets["EXPORT_PROVIDER"] = "notes"
        _add_list_item(shopping_storage, "milk")

        command.run(request_info, provider="walmart")
        payload = post_mock.call_args.args[0]
        assert payload["metadata"]["context"] == {"provider": "walmart"}

    def test_over_cap_list_truncates_to_100_rows_regulars_first(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """101+ open items must not raise out of run() — the producer drops
        overflow in document order (Regulars first), mirroring the renderer.
        Overflow items stay on the list and ride along on the next export."""
        for i in range(3):
            _add_list_item(shopping_storage, f"regular {i}", regular=True)
        for i in range(98):
            _add_list_item(shopping_storage, f"one off {i}")

        response = command.run(request_info)
        assert response.success

        metadata = post_mock.call_args.args[0]["metadata"]
        assert [s["title"] for s in metadata["sections"]] == ["Regulars", "One-offs"]
        assert len(metadata["sections"][0]["rows"]) == 3
        assert len(metadata["sections"][1]["rows"]) == 97
        total = sum(len(s["rows"]) for s in metadata["sections"])
        assert total == 100


class TestPreSeeding:
    def test_unmatched_items_pre_seed_map_rows(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "almond butter")

        command.run(request_info)
        seeded = map_storage.get("almond_butter")
        assert seeded == {"item": "almond butter", "walmart_item_id": ""}

    def test_existing_map_rows_not_overwritten(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "10450114")

        command.run(request_info)
        assert map_storage.get("milk")["walmart_item_id"] == "10450114"

    def test_map_rows_never_carry_user_id(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """The staples map is household-shared — no per-user scoping."""
        _add_list_item(shopping_storage, "milk")
        command.run(request_info)
        assert "user_id" not in map_storage.get("milk")

    def test_notes_provider_does_not_pre_seed(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Notes households shouldn't accumulate walmart_items map noise."""
        _add_list_item(shopping_storage, "almond butter")

        response = command.run(request_info, provider="notes")
        assert response.success
        assert map_storage.get("almond_butter") is None

    def test_notes_provider_leaves_existing_map_rows_intact(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Even a mapped item renders ungated for notes — and the map row
        survives the export untouched."""
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "10450114")

        command.run(request_info, provider="notes")
        payload = post_mock.call_args.args[0]
        (row,) = _all_rows(payload["metadata"])
        assert "requires_record_field" not in row
        assert "row_actions" not in row
        assert map_storage.get("milk")["walmart_item_id"] == "10450114"


class TestRunMessages:
    def test_success_message_with_unmatched(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "milk", "10450114")

        response = command.run(request_info)
        message = response.context_data["message"]
        assert "I sent your shopping list to your phone" in message
        assert "2 items ready" in message
        assert "1 still needs a Walmart match" in message

    def test_success_message_fully_matched(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "10450114")

        response = command.run(request_info)
        message = response.context_data["message"]
        assert "1 item ready" in message
        assert "Walmart match" not in message

    def test_notes_message_has_no_walmart_tail(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        """Notes rows are all exportable — never mention Walmart matches."""
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "candles")

        response = command.run(request_info, provider="notes")
        message = response.context_data["message"]
        assert message == "I sent your shopping list to your phone — 2 items."

    def test_notes_message_singular(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")

        response = command.run(request_info, provider="notes")
        assert response.context_data["message"] == (
            "I sent your shopping list to your phone — 1 item."
        )

    @pytest.mark.parametrize("reason", ["http_error", "no_cc_url", "import_error"])
    def test_post_failure_maps_to_spoken_error(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        request_info: RequestInformation,
        reason: str,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        with patch.object(
            ExportShoppingListCommand, "_post_inbox_item", return_value=reason
        ):
            response = command.run(request_info)
        assert not response.success
        assert response.context_data["message"]


class TestCallback:
    def test_builds_cart_url_from_mapped_ids(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "eggs")
        _add_map_entry(map_storage, "milk", "111")
        _add_map_entry(map_storage, "eggs", "222")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}, {"key": "eggs", "quantity": 1}]}, request_info
        )
        assert response.success
        assert response.context_data["url"] == (
            "https://affil.walmart.com/cart/addToCart?items=111|1,222|1"
        )
        # detail_lines is the generic result-view key; exported is the
        # legacy key older clients read — both carry the display names.
        assert response.context_data["exported"] == ["milk", "eggs"]
        assert response.context_data["detail_lines"] == ["milk", "eggs"]
        assert "Exported 2 items to your Walmart cart" in response.context_data["message"]

    def test_exported_one_off_removed_from_list(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """Export is the trip boundary: one-offs leave the list."""
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        command.export_selected({"selected": [{"key": "milk", "quantity": 1}]}, request_info)
        assert shopping_storage.get("milk") is None

    def test_exported_regular_reset_to_unchecked(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """Exported regulars come straight back on the list for next trip."""
        _add_list_item(shopping_storage, "milk", regular=True)
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}]}, request_info
        )
        record = shopping_storage.get("milk")
        assert record is not None
        assert record["checked"] is False
        assert record["checked_at"] is None
        assert record["regular"] is True
        assert "regular is back on the list" in response.context_data["message"]

    def test_deselected_items_untouched(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """A partial export must never delete what the user chose to keep."""
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "eggs")
        _add_map_entry(map_storage, "milk", "111")
        _add_map_entry(map_storage, "eggs", "222")

        command.export_selected({"selected": [{"key": "milk", "quantity": 1}]}, request_info)
        assert shopping_storage.get("milk") is None
        eggs = shopping_storage.get("eggs")
        assert eggs is not None
        assert eggs["checked"] is False

    def test_checked_write_back_strips_meta_fields(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk", regular=True)
        # Simulate a record that somehow carries meta fields (the
        # _normalize_record hygiene rule: never persist them).
        polluted = shopping_storage.get("milk")
        polluted["_data_key"] = "milk"
        polluted["_expires_at"] = None
        polluted["data_key"] = "milk"
        shopping_storage.save("milk", polluted)
        _add_map_entry(map_storage, "milk", "111")

        command.export_selected({"selected": [{"key": "milk", "quantity": 1}]}, request_info)
        record = shopping_storage.get("milk")
        assert "_data_key" not in record
        assert "_expires_at" not in record
        assert "data_key" not in record

    def test_skips_unmapped_items(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "milk", "111")
        _add_map_entry(map_storage, "candles", "")  # pre-seeded, never filled

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}, {"key": "candles", "quantity": 1}]}, request_info
        )
        assert response.success
        assert response.context_data["url"].endswith("items=111|1")
        assert response.context_data["exported"] == ["milk"]
        assert response.context_data["detail_lines"] == ["milk"]
        # Unmapped items were NOT exported, so they stay on the list
        assert shopping_storage.get("candles")["checked"] is False

    def test_url_carries_quantities(self) -> None:
        entries = [
            {"item": "milk", "walmart_item_id": "42", "quantity": 2},
            {"item": "eggs", "walmart_item_id": "7", "quantity": 1},
        ]
        assert _build_walmart_result(entries) == {
            "url": "https://affil.walmart.com/cart/addToCart?items=42|2,7|1"
        }

    def test_walmart_builder_skips_unmapped_entries(self) -> None:
        entries = [
            {"item": "milk", "walmart_item_id": "42", "quantity": 1},
            {"item": "candles", "walmart_item_id": None, "quantity": 1},
        ]
        assert _build_walmart_result(entries) == {
            "url": "https://affil.walmart.com/cart/addToCart?items=42|1"
        }

    def test_notes_builder_text_shape(self) -> None:
        entries = [
            {"item": "milk", "walmart_item_id": "42", "quantity": 2},
            {"item": "bread", "walmart_item_id": None, "quantity": 1},
        ]
        # All entries included (no IDs needed); " x2" suffix only when > 1.
        assert _build_notes_result(entries) == {
            "text": "Shopping list:\n- milk x2\n- bread"
        }

    def test_callback_quantity_flows_into_url(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")
        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 3}]}, request_info
        )
        assert response.context_data["url"].endswith("items=111|3")

    def test_callback_quantity_clamped(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")
        for bogus, expected in [(0, 1), (-5, 1), ("nope", 1), (500, 99), (None, 1)]:
            response = command.export_selected(
                {"selected": [{"key": "milk", "quantity": bogus}]}, request_info
            )
            assert response.context_data["url"].endswith(f"items=111|{expected}"), bogus

    def test_rows_carry_default_quantity(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        key = _add_list_item(shopping_storage, "milk")
        record = shopping_storage.get(key)
        record["quantity"] = 2
        shopping_storage.save(key, record)
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "milk", "111")

        command.run(request_info)
        payload = post_mock.call_args.args[0]
        by_key = {r["key"]: r for r in _all_rows(payload["metadata"])}
        assert by_key["milk"]["default"] == {"selected": True, "quantity": 2}
        assert by_key["candles"]["default"] == {"selected": True, "quantity": 1}

    def test_empty_selected_is_error(
        self,
        command: ExportShoppingListCommand,
        request_info: RequestInformation,
    ) -> None:
        response = command.export_selected({"selected": []}, request_info)
        assert not response.success
        assert response.context_data["message"]

    def test_missing_selected_is_error(
        self,
        command: ExportShoppingListCommand,
        request_info: RequestInformation,
    ) -> None:
        response = command.export_selected({}, request_info)
        assert not response.success
        assert response.context_data["message"]

    def test_zero_resolvable_ids_is_error(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "candles", "")

        response = command.export_selected(
            {"selected": [{"key": "candles", "quantity": 1}]}, request_info
        )
        assert not response.success
        assert (
            response.context_data["message"]
            == "None of the selected items have a Walmart ID yet."
        )
        # Nothing was exported, so nothing gets checked off
        assert shopping_storage.get("candles")["checked"] is False

    def test_unknown_keys_are_skipped(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}, {"key": "ghost_item", "quantity": 1}]}, request_info
        )
        assert response.success
        assert response.context_data["exported"] == ["milk"]

    def test_walmart_result_has_url_not_text(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """Exactly one of url/text is set per the shared contract."""
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}], "provider": "walmart"},
            request_info,
        )
        assert "url" in response.context_data
        assert "text" not in response.context_data


class TestNotesCallback:
    def test_notes_exports_all_selected_including_unmapped(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "milk", "111")  # candles has no map row

        response = command.export_selected(
            {
                "selected": [
                    {"key": "milk", "quantity": 2},
                    {"key": "candles", "quantity": 1},
                ],
                "context": {"provider": "notes"},
            },
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == (
            "Shopping list:\n- milk x2\n- candles"
        )
        assert response.context_data["exported"] == ["milk", "candles"]
        assert response.context_data["detail_lines"] == ["milk", "candles"]
        assert "url" not in response.context_data
        assert response.context_data["message"] == (
            "Your list is ready to copy — 2 items."
        )

    def test_notes_message_singular(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}], "context": {"provider": "notes"}},
            request_info,
        )
        assert response.context_data["message"] == (
            "Your list is ready to copy — 1 item."
        )

    def test_notes_marks_all_exported_checked(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """Copying the list = the trip is happening — unmapped items too."""
        _add_list_item(shopping_storage, "milk")
        _add_list_item(shopping_storage, "candles")

        command.export_selected(
            {
                "selected": [
                    {"key": "milk", "quantity": 1},
                    {"key": "candles", "quantity": 1},
                ],
                "context": {"provider": "notes"},
            },
            request_info,
        )
        # One-offs leave the list when exported, same as walmart.
        assert shopping_storage.get("milk") is None
        assert shopping_storage.get("candles") is None

    def test_notes_zero_mapped_is_not_an_error(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """The walmart 'no Walmart ID' error can't happen for notes."""
        _add_list_item(shopping_storage, "candles")
        _add_map_entry(map_storage, "candles", "")

        response = command.export_selected(
            {"selected": [{"key": "candles", "quantity": 1}], "context": {"provider": "notes"}},
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == "Shopping list:\n- candles"

    def test_notes_display_name_falls_back_to_key(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        response = command.export_selected(
            {"selected": [{"key": "ghost_item", "quantity": 1}], "context": {"provider": "notes"}},
            request_info,
        )
        assert response.success
        assert response.context_data["exported"] == ["ghost_item"]

    def test_notes_empty_selection_is_error(
        self,
        command: ExportShoppingListCommand,
        request_info: RequestInformation,
    ) -> None:
        response = command.export_selected(
            {"selected": [], "context": {"provider": "notes"}}, request_info
        )
        assert not response.success
        assert response.context_data["message"]


class TestCallbackProviderEcho:
    """Resolution order: data['context']['provider'] (the interactive-list
    payload's context, echoed verbatim by mobile) → legacy top-level
    data['provider'] (pre-interactive-list clients) → configured secret."""

    def test_context_provider_overrides_configured(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """Result must match what the interactive list rendered, not the secret."""
        map_storage._secrets["EXPORT_PROVIDER"] = "walmart"
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected(
            {
                "selected": [{"key": "milk", "quantity": 1}],
                "context": {"provider": "notes"},
            },
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == "Shopping list:\n- milk"
        assert "url" not in response.context_data

    def test_context_provider_wins_over_legacy_key(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected(
            {
                "selected": [{"key": "milk", "quantity": 1}],
                "context": {"provider": "notes"},
                "provider": "walmart",
            },
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == "Shopping list:\n- milk"
        assert "url" not in response.context_data

    def test_legacy_provider_key_still_honored(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """Back-compat: pre-interactive-list clients send a top-level provider."""
        map_storage._secrets["EXPORT_PROVIDER"] = "walmart"
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}], "provider": "notes"},
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == "Shopping list:\n- milk"
        assert "url" not in response.context_data

    def test_invalid_context_provider_falls_back_to_configured(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        map_storage._secrets["EXPORT_PROVIDER"] = "notes"
        _add_list_item(shopping_storage, "milk")

        response = command.export_selected(
            {
                "selected": [{"key": "milk", "quantity": 1}],
                "context": {"provider": "amazon"},
            },
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == "Shopping list:\n- milk"

    def test_invalid_legacy_provider_falls_back_to_configured(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        map_storage._secrets["EXPORT_PROVIDER"] = "notes"
        _add_list_item(shopping_storage, "milk")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}], "provider": "amazon"},
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == "Shopping list:\n- milk"

    def test_non_dict_context_is_ignored(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        map_storage._secrets["EXPORT_PROVIDER"] = "notes"
        _add_list_item(shopping_storage, "milk")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}], "context": "notes"},
            request_info,
        )
        assert response.success
        assert response.context_data["text"] == "Shopping list:\n- milk"

    def test_missing_provider_falls_back_to_default_walmart(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected(
            {"selected": [{"key": "milk", "quantity": 1}]}, request_info
        )
        assert response.success
        assert response.context_data["url"].startswith("https://affil.walmart.com/")


class TestProviders:
    def test_providers_registry(self) -> None:
        assert PROVIDERS["walmart"] is _build_walmart_result
        assert PROVIDERS["notes"] is _build_notes_result
        assert sorted(PROVIDERS) == ["notes", "walmart"]

    def test_unknown_provider_secret_falls_back_to_walmart(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        map_storage._secrets["EXPORT_PROVIDER"] = "amazon"
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected({"selected": [{"key": "milk", "quantity": 1}]}, request_info)
        assert response.success
        assert response.context_data["url"].startswith("https://affil.walmart.com/")

    def test_explicit_walmart_provider_secret(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        map_storage._secrets["EXPORT_PROVIDER"] = "walmart"
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")

        response = command.export_selected({"selected": [{"key": "milk", "quantity": 1}]}, request_info)
        assert response.success
        assert response.context_data["url"].startswith("https://affil.walmart.com/")


class TestMessageContract:
    """Every run()/callback outcome must set context_data['message'] —
    pre-routed responses without it silently fall back to the LLM."""

    def test_speaker_gate_sets_message(
        self,
        command: ExportShoppingListCommand,
        post_mock,
        request_info_unknown_speaker: RequestInformation,
    ) -> None:
        response = command.run(request_info_unknown_speaker)
        assert response.context_data.get("message")

    def test_empty_list_sets_message(
        self,
        command: ExportShoppingListCommand,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info)
        assert response.context_data.get("message")

    def test_success_sets_message(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        response = command.run(request_info)
        assert response.context_data.get("message")

    def test_post_failure_sets_message(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        with patch.object(
            ExportShoppingListCommand, "_post_inbox_item", return_value="http_error"
        ):
            response = command.run(request_info)
        assert response.context_data.get("message")

    def test_callback_success_sets_message(
        self,
        command: ExportShoppingListCommand,
        shopping_storage: FakeStorage,
        map_storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_list_item(shopping_storage, "milk")
        _add_map_entry(map_storage, "milk", "111")
        response = command.export_selected({"selected": [{"key": "milk", "quantity": 1}]}, request_info)
        assert response.context_data.get("message")

    def test_callback_errors_set_message(
        self,
        command: ExportShoppingListCommand,
        request_info: RequestInformation,
    ) -> None:
        response = command.export_selected({"selected": []}, request_info)
        assert response.context_data.get("message")
