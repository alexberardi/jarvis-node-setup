"""Tests for ShoppingListCommand — add, read, check_off, remove, clear actions."""

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

import commands.shopping_list_command as slc
from commands.shopping_list_command import (
    ShoppingListCommand,
    _item_key,
    _split_items,
    _spoken_join,
)
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


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def command(storage: FakeStorage):
    with patch.object(slc, "_get_storage", return_value=storage):
        yield ShoppingListCommand()


@pytest.fixture
def request_info() -> RequestInformation:
    return RequestInformation(
        voice_command="test",
        conversation_id="conv-1",
        user_id=_DEFAULT_USER_ID,
    )


@pytest.fixture
def request_info_unknown_speaker() -> RequestInformation:
    return RequestInformation(
        voice_command="test",
        conversation_id="conv-1",
        user_id=None,
    )


class TestHelpers:
    def test_split_compound_items(self) -> None:
        assert _split_items("milk, eggs, and butter") == ["milk", "eggs", "butter"]

    def test_split_two_items_with_and(self) -> None:
        assert _split_items("apples and oranges") == ["apples", "oranges"]

    def test_split_strips_leading_filler(self) -> None:
        assert _split_items("some milk and a thing of butter") == ["milk", "butter"]

    def test_split_single_item(self) -> None:
        assert _split_items("paper towels") == ["paper towels"]

    def test_item_key_normalizes(self) -> None:
        assert _item_key("Almond Butter!") == "almond_butter"

    def test_spoken_join_three(self) -> None:
        assert _spoken_join(["milk", "eggs", "butter"]) == "milk, eggs, and butter"

    def test_spoken_join_two(self) -> None:
        assert _spoken_join(["milk", "eggs"]) == "milk and eggs"


class TestProperties:
    def test_command_name(self, command: ShoppingListCommand) -> None:
        assert command.command_name == "shopping_list"

    def test_keywords_include_shopping_list(self, command: ShoppingListCommand) -> None:
        assert "shopping list" in command.keywords

    def test_action_enum_covers_all_actions(self, command: ShoppingListCommand) -> None:
        action = next(p for p in command.parameters if p.name == "action")
        assert set(action.enum_values) == {"add", "read", "check_off", "remove", "clear"}

    def test_items_is_string_array(self, command: ShoppingListCommand) -> None:
        items = next(p for p in command.parameters if p.name == "items")
        assert items.param_type == "array<string>"

    def test_exactly_one_primary_prompt_example(self, command: ShoppingListCommand) -> None:
        primaries = [e for e in command.generate_prompt_examples() if e.is_primary]
        assert len(primaries) == 1

    def test_adapter_examples_count(self, command: ShoppingListCommand) -> None:
        assert 10 <= len(command.generate_adapter_examples()) <= 25


class TestDataBrowser:
    def test_editable_fields(self, command: ShoppingListCommand) -> None:
        fields = {f.name: f for f in command.editable_fields()}
        # item is read-only on mobile: renaming would desync it from the storage key
        assert not fields["item"].editable
        assert fields["checked"].editable
        assert fields["regular"].editable
        assert not fields["added_at"].editable

    def test_display_summary_unchecked(self, command: ShoppingListCommand) -> None:
        summary = command.display_summary({"item": "milk", "checked": False})
        assert summary.title == "milk"

    def test_display_summary_checked(self, command: ShoppingListCommand) -> None:
        summary = command.display_summary({"item": "milk", "checked": True})
        assert summary.title == "✓ milk"
        assert summary.subtitle == "Checked off"

    def test_display_summary_tolerates_empty_record(self, command: ShoppingListCommand) -> None:
        summary = command.display_summary({})
        assert summary.title


class TestAdd:
    def test_add_single_item(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="add", items=["milk"])
        assert response.success
        assert "Added milk" in response.context_data["message"]
        record = storage.get("milk")
        assert record["item"] == "milk"
        assert record["checked"] is False
        assert record["added_by"] == _DEFAULT_USER_ID

    def test_add_multiple_items(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="add", items=["milk", "eggs", "butter"])
        assert response.success
        assert "milk, eggs, and butter" in response.context_data["message"]
        assert len(storage.get_all()) == 3

    def test_add_duplicate_reports_already_on_list(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"])
        response = command.run(request_info, action="add", items=["milk"])
        assert response.success
        assert "already on" in response.context_data["message"]

    def test_add_checked_item_unchecks_it(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"])
        command.run(request_info, action="check_off", items=["milk"])
        response = command.run(request_info, action="add", items=["milk"])
        assert response.success
        assert storage.get("milk")["checked"] is False

    def test_add_without_recognized_speaker_still_works(
        self,
        command: ShoppingListCommand,
        storage: FakeStorage,
        request_info_unknown_speaker: RequestInformation,
    ) -> None:
        response = command.run(request_info_unknown_speaker, action="add", items=["milk"])
        assert response.success
        assert "added_by" not in storage.get("milk")

    def test_records_never_carry_user_id_field(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        """user_id on a record would scope it per-user in the data browser."""
        command.run(request_info, action="add", items=["milk"])
        assert "user_id" not in storage.get("milk")

    def test_add_missing_items_asks_which(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="add", items=[])
        assert not response.success
        assert response.wait_for_input
        assert response.context_data["message"]


class TestRead:
    def test_read_empty_list(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="read")
        assert response.success
        assert "empty" in response.context_data["message"]

    def test_read_lists_open_items(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk", "eggs"])
        response = command.run(request_info, action="read")
        assert response.success
        assert "2 items" in response.context_data["message"]
        assert set(response.context_data["items"]) == {"milk", "eggs"}

    def test_read_excludes_checked_items(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk", "eggs"])
        command.run(request_info, action="check_off", items=["milk"])
        response = command.run(request_info, action="read")
        assert response.context_data["items"] == ["eggs"]


class TestCheckOff:
    def test_check_off_marks_checked(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"])
        response = command.run(request_info, action="check_off", items=["milk"])
        assert response.success
        assert "Checked off milk" in response.context_data["message"]
        record = storage.get("milk")
        assert record["checked"] is True
        assert record["checked_at"]

    def test_check_off_substring_match(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["whole milk"])
        response = command.run(request_info, action="check_off", items=["milk"])
        assert response.success
        assert storage.get("whole_milk")["checked"] is True

    def test_check_off_missing_item(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="check_off", items=["caviar"])
        assert response.success
        assert "couldn't find caviar" in response.context_data["message"]


class TestRemove:
    def test_remove_deletes_record(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"])
        response = command.run(request_info, action="remove", items=["milk"])
        assert response.success
        assert storage.get("milk") is None

    def test_remove_missing_item(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="remove", items=["caviar"])
        assert response.success
        assert "couldn't find" in response.context_data["message"]


class TestClear:
    def test_clear_removes_one_offs(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk", "eggs", "butter"])
        response = command.run(request_info, action="clear")
        assert response.success
        assert "3 items" in response.context_data["message"]
        assert storage.get_all() == []

    def test_clear_empty_list(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="clear")
        assert response.success
        assert "already empty" in response.context_data["message"]


class TestRegulars:
    def test_add_as_regular(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        response = command.run(request_info, action="add", items=["milk"], regular=True)
        assert response.success
        assert "as a regular" in response.context_data["message"]
        assert storage.get("milk")["regular"] is True

    def test_promote_existing_item_to_regular(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"])
        response = command.run(request_info, action="add", items=["milk"], regular=True)
        assert response.success
        assert "Made milk a regular" in response.context_data["message"]
        assert storage.get("milk")["regular"] is True

    def test_readd_without_flag_does_not_demote(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"], regular=True)
        command.run(request_info, action="check_off", items=["milk"])
        command.run(request_info, action="add", items=["milk"])
        assert storage.get("milk")["regular"] is True

    def test_trip_clear_resets_regulars_and_deletes_one_offs(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"], regular=True)
        command.run(request_info, action="add", items=["candles"])
        command.run(request_info, action="check_off", items=["milk"])
        response = command.run(request_info, action="clear")
        assert response.success
        assert "regular is back on it" in response.context_data["message"]
        milk = storage.get("milk")
        assert milk["checked"] is False
        assert milk["checked_at"] is None
        assert milk["regular"] is True
        assert "data_key" not in milk
        assert storage.get("candles") is None

    def test_clear_everything_wipes_regulars(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"], regular=True)
        command.run(request_info, action="add", items=["candles"])
        response = command.run(request_info, action="clear", scope="everything")
        assert response.success
        assert "including regulars" in response.context_data["message"]
        assert storage.get_all() == []

    def test_read_includes_unchecked_regulars(
        self, command: ShoppingListCommand, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["milk"], regular=True)
        command.run(request_info, action="clear")
        response = command.run(request_info, action="read")
        assert response.context_data["items"] == ["milk"]

    def test_display_summary_regular_subtitle(self, command: ShoppingListCommand) -> None:
        summary = command.display_summary({"item": "milk", "checked": False, "regular": True})
        assert summary.subtitle == "Regular"
        checked = command.display_summary({"item": "milk", "checked": True, "regular": True})
        assert checked.subtitle == "Checked off • Regular"


class TestFuzzyMatchMeta:
    def test_fuzzy_check_off_uses_real_storage_key(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        """Substring-matched records must update in place, not fork a duplicate."""
        command.run(request_info, action="add", items=["whole milk"])
        response = command.run(request_info, action="check_off", items=["milk"])
        assert response.success
        assert len(storage._data) == 1
        record = storage.get("whole_milk")
        assert record["checked"] is True
        assert "_data_key" not in record
        assert "_expires_at" not in record
        assert "data_key" not in record

    def test_fuzzy_match_is_deterministic(
        self, command: ShoppingListCommand, storage: FakeStorage, request_info: RequestInformation
    ) -> None:
        command.run(request_info, action="add", items=["almond milk", "almond butter"])
        response = command.run(request_info, action="check_off", items=["almond"])
        assert response.success
        # Key order: almond_butter sorts before almond_milk
        assert storage.get("almond_butter")["checked"] is True
        assert storage.get("almond_milk")["checked"] is False


class TestPostProcess:
    def test_string_items_split(self, command: ShoppingListCommand) -> None:
        args = command.post_process_tool_call(
            {"action": "add", "items": "milk, eggs, and butter"}, "add milk eggs and butter"
        )
        assert args["items"] == ["milk", "eggs", "butter"]

    def test_array_entries_preserved_verbatim(self, command: ShoppingListCommand) -> None:
        """LLM-segmented arrays are respected — never re-split compound names."""
        args = command.post_process_tool_call(
            {"action": "add", "items": ["mac and cheese"]}, "add mac and cheese"
        )
        assert args["items"] == ["mac and cheese"]

    def test_string_regular_coerced_to_bool(self, command: ShoppingListCommand) -> None:
        args = command.post_process_tool_call(
            {"action": "add", "items": ["milk"], "regular": "true"}, "add milk as a regular"
        )
        assert args["regular"] is True

    def test_invalid_scope_dropped(self, command: ShoppingListCommand) -> None:
        args = command.post_process_tool_call(
            {"action": "clear", "scope": "bogus"}, "clear the list"
        )
        assert "scope" not in args

    def test_missing_action_defaults_to_add_with_items(self, command: ShoppingListCommand) -> None:
        args = command.post_process_tool_call({"items": ["milk"]}, "milk on the list")
        assert args["action"] == "add"

    def test_missing_action_defaults_to_read_without_items(self, command: ShoppingListCommand) -> None:
        args = command.post_process_tool_call({}, "shopping list")
        assert args["action"] == "read"


class TestMessageContract:
    """Every run() outcome must set context_data['message'] — pre-routed
    responses without it silently fall back to the LLM."""

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"action": "add", "items": ["milk"]},
            {"action": "read"},
            {"action": "check_off", "items": ["milk"]},
            {"action": "remove", "items": ["milk"]},
            {"action": "clear"},
        ],
    )
    def test_every_action_sets_message(
        self, command: ShoppingListCommand, request_info: RequestInformation, kwargs: dict
    ) -> None:
        response = command.run(request_info, **kwargs)
        assert response.context_data.get("message")
