"""Tests for TodoListCommand — add, read, complete, uncomplete, remove, clear."""

from typing import Any, Dict, List
from unittest.mock import patch

import pytest

import commands.todo_list_command as tlc
from commands.todo_list_command import (
    TodoListCommand,
    _item_key,
    _spoken_join,
)
from core.request_information import RequestInformation


_DEFAULT_USER_ID = 42


class FakeStorage:
    """In-memory stand-in for JarvisStorage.

    Mirrors CommandDataRepository.get_all exactly: meta fields are
    underscore-prefixed (`_data_key`, `_expires_at`) — the real backend does
    NOT inject a bare `data_key`.
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
    with patch.object(tlc, "_get_storage", return_value=storage):
        yield TodoListCommand()


@pytest.fixture
def request_info() -> RequestInformation:
    return RequestInformation(
        voice_command="test",
        conversation_id="conv-1",
        user_id=_DEFAULT_USER_ID,
    )


def _add_task(
    storage: FakeStorage,
    task: str,
    *,
    completed: bool = False,
    added_at: str = "2026-06-09T12:00:00+00:00",
    completed_at: str | None = None,
) -> str:
    key = _item_key(task)
    storage.save(key, {
        "task": task,
        "completed": completed,
        "added_at": added_at,
        "completed_at": completed_at,
    })
    return key


class TestHelpers:
    def test_item_key_slugifies(self) -> None:
        assert _item_key("Call the Dentist") == "call_the_dentist"

    def test_spoken_join(self) -> None:
        assert _spoken_join(["a"]) == "a"
        assert _spoken_join(["a", "b"]) == "a and b"
        assert _spoken_join(["a", "b", "c"]) == "a, b, and c"


class TestProperties:
    def test_command_name(self, command: TodoListCommand) -> None:
        assert command.command_name == "todo_list"

    def test_data_browser_storage_name_defaults_to_command_name(
        self, command: TodoListCommand
    ) -> None:
        assert command.data_browser_storage_name == "todo_list"

    def test_action_enum(self, command: TodoListCommand) -> None:
        action = next(p for p in command.parameters if p.name == "action")
        assert set(action.enum_values) == {
            "add", "read", "complete", "uncomplete", "remove", "clear"
        }

    def test_antipattern_points_to_reminder(self, command: TodoListCommand) -> None:
        assert any(ap.command_name == "reminder" for ap in command.antipatterns)

    def test_exactly_one_primary_prompt_example(self, command: TodoListCommand) -> None:
        primaries = [e for e in command.generate_prompt_examples() if e.is_primary]
        assert len(primaries) == 1

    def test_adapter_examples_count(self, command: TodoListCommand) -> None:
        assert 10 <= len(command.generate_adapter_examples()) <= 25


class TestPostProcess:
    def test_string_task_wrapped_not_split(self, command: TodoListCommand) -> None:
        """A single task is never split on 'and'."""
        args = command.post_process_tool_call(
            {"action": "add", "tasks": "call mom and dad"}, "..."
        )
        assert args["tasks"] == ["call mom and dad"]

    def test_list_tasks_stripped_not_resplit(self, command: TodoListCommand) -> None:
        args = command.post_process_tool_call(
            {"action": "add", "tasks": ["  walk the dog ", "buy milk and bread"]}, "..."
        )
        assert args["tasks"] == ["walk the dog", "buy milk and bread"]

    def test_default_action_add_when_tasks(self, command: TodoListCommand) -> None:
        args = command.post_process_tool_call({"tasks": ["x"]}, "...")
        assert args["action"] == "add"

    def test_default_action_read_when_no_tasks(self, command: TodoListCommand) -> None:
        args = command.post_process_tool_call({}, "...")
        assert args["action"] == "read"

    def test_invalid_scope_dropped(self, command: TodoListCommand) -> None:
        args = command.post_process_tool_call(
            {"action": "clear", "scope": "garbage"}, "..."
        )
        assert "scope" not in args

    def test_valid_scope_kept(self, command: TodoListCommand) -> None:
        args = command.post_process_tool_call(
            {"action": "clear", "scope": "completed"}, "..."
        )
        assert args["scope"] == "completed"


class TestAdd:
    def test_add_single(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info, action="add", tasks=["call the dentist"])
        assert response.success
        assert "Added call the dentist" in response.context_data["message"]
        record = storage.get("call_the_dentist")
        assert record["task"] == "call the dentist"
        assert record["completed"] is False
        assert record["completed_at"] is None
        assert record["added_by"] == _DEFAULT_USER_ID

    def test_add_keeps_compound_phrase_as_one(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        command.run(request_info, action="add", tasks=["call mom and dad"])
        assert storage.get("call_mom_and_dad")["task"] == "call mom and dad"

    def test_add_duplicate_open_is_noop(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "fold the laundry")
        response = command.run(request_info, action="add", tasks=["fold the laundry"])
        assert "already on it" in response.context_data["message"]

    def test_readding_completed_task_reopens_it(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "water the plants", completed=True,
                  completed_at="2026-06-01T00:00:00+00:00")
        command.run(request_info, action="add", tasks=["water the plants"])
        record = storage.get("water_the_plants")
        assert record["completed"] is False
        assert record["completed_at"] is None

    def test_add_missing_tasks_asks(
        self, command: TodoListCommand, request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info, action="add", tasks=[])
        assert not response.success
        assert response.wait_for_input
        assert response.context_data["message"] == "Which task?"


class TestRead:
    def test_read_empty(
        self, command: TodoListCommand, request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info, action="read")
        assert response.context_data["message"] == "Your to-do list is empty."

    def test_read_single(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "call the dentist")
        response = command.run(request_info, action="read")
        assert "one thing" in response.context_data["message"]
        assert "call the dentist" in response.context_data["message"]

    def test_read_excludes_completed(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        _add_task(storage, "done task", completed=True)
        response = command.run(request_info, action="read")
        assert response.context_data["tasks"] == ["open task"]

    def test_read_sorted_by_added_at(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "second", added_at="2026-06-09T13:00:00+00:00")
        _add_task(storage, "first", added_at="2026-06-09T12:00:00+00:00")
        response = command.run(request_info, action="read")
        assert response.context_data["tasks"] == ["first", "second"]


class TestComplete:
    def test_complete_exact(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "fold the laundry")
        response = command.run(request_info, action="complete", tasks=["fold the laundry"])
        assert "Marked fold the laundry done" in response.context_data["message"]
        record = storage.get("fold_the_laundry")
        assert record["completed"] is True
        assert record["completed_at"] is not None

    def test_complete_substring_match(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "call the dentist")
        response = command.run(request_info, action="complete", tasks=["the dentist"])
        assert response.success
        assert storage.get("call_the_dentist")["completed"] is True

    def test_complete_missing(
        self, command: TodoListCommand, request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info, action="complete", tasks=["ghost"])
        assert "couldn't find ghost" in response.context_data["message"]

    def test_complete_write_back_strips_meta(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "wash the car")
        command.run(request_info, action="complete", tasks=["wash the car"])
        record = storage.get("wash_the_car")
        assert "_data_key" not in record
        assert "data_key" not in record
        assert "_expires_at" not in record


class TestUncomplete:
    def test_uncomplete_reopens(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "fold the laundry", completed=True,
                  completed_at="2026-06-10T00:00:00+00:00")
        response = command.run(request_info, action="uncomplete", tasks=["fold the laundry"])
        assert "Marked fold the laundry not done" in response.context_data["message"]
        record = storage.get("fold_the_laundry")
        assert record["completed"] is False
        assert record["completed_at"] is None


class TestRemove:
    def test_remove_exact(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "fold the laundry")
        response = command.run(request_info, action="remove", tasks=["fold the laundry"])
        assert "Removed fold the laundry" in response.context_data["message"]
        assert storage.get("fold_the_laundry") is None

    def test_remove_missing(
        self, command: TodoListCommand, request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info, action="remove", tasks=["ghost"])
        assert "couldn't find ghost" in response.context_data["message"]


class TestClear:
    def test_clear_all(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "a")
        _add_task(storage, "b", completed=True)
        response = command.run(request_info, action="clear")
        assert "removed 2 tasks" in response.context_data["message"]
        assert storage.get_all() == []

    def test_clear_all_when_empty(
        self, command: TodoListCommand, request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info, action="clear")
        assert "already empty" in response.context_data["message"]

    def test_clear_completed_only(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open one")
        _add_task(storage, "done one", completed=True)
        response = command.run(request_info, action="clear", scope="completed")
        assert "one completed task" in response.context_data["message"]
        assert storage.get("open_one") is not None
        assert storage.get("done_one") is None

    def test_clear_completed_when_none_done(
        self, command: TodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open one")
        response = command.run(request_info, action="clear", scope="completed")
        assert "no completed tasks" in response.context_data["message"]
        assert storage.get("open_one") is not None


class TestDataBrowser:
    def test_editable_fields(self, command: TodoListCommand) -> None:
        fields = {f.name: f for f in command.editable_fields()}
        assert not fields["task"].editable
        assert fields["task"].required
        assert fields["completed"].editable

    def test_display_summary_open(self, command: TodoListCommand) -> None:
        summary = command.display_summary({"task": "call the dentist", "completed": False})
        assert summary.title == "call the dentist"
        assert summary.subtitle is None

    def test_display_summary_done(self, command: TodoListCommand) -> None:
        summary = command.display_summary({"task": "call the dentist", "completed": True})
        assert summary.title == "✓ call the dentist"
        assert summary.subtitle == "Done"

    def test_display_summary_tolerates_empty_record(self, command: TodoListCommand) -> None:
        summary = command.display_summary({})
        assert summary.title


class TestMessageContract:
    """Every run() outcome must set context_data['message'] — pre-routed
    responses without it silently fall back to the LLM."""

    @pytest.mark.parametrize("kwargs", [
        {"action": "read"},
        {"action": "add", "tasks": ["x"]},
        {"action": "add", "tasks": []},
        {"action": "complete", "tasks": ["ghost"]},
        {"action": "uncomplete", "tasks": ["ghost"]},
        {"action": "remove", "tasks": ["ghost"]},
        {"action": "clear"},
        {"action": "clear", "scope": "completed"},
    ])
    def test_message_always_set(
        self, command: TodoListCommand, request_info: RequestInformation,
        kwargs: Dict[str, Any],
    ) -> None:
        response = command.run(request_info, **kwargs)
        assert response.context_data.get("message")
