"""Tests for ExportTodoListCommand — interactive checklist + save_todo_state callback."""

from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

import commands.export_todo_list_command as etlc
from commands.export_todo_list_command import ExportTodoListCommand
from commands.todo_list_command import _item_key
from core.request_information import RequestInformation


_DEFAULT_USER_ID = 42


class FakeStorage:
    """In-memory stand-in for JarvisStorage. Mirrors CommandDataRepository
    .get_all exactly: meta fields are underscore-prefixed."""

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
    # logger is mocked so failure-path tests don't leave the log client's
    # background flush thread writing to pytest's closed capture streams.
    with patch.object(etlc, "_get_storage", return_value=storage), \
         patch.object(etlc, "logger", MagicMock()):
        yield ExportTodoListCommand()


@pytest.fixture
def post_mock():
    with patch.object(
        ExportTodoListCommand, "_post_inbox_item", return_value="ok"
    ) as mock:
        yield mock


@pytest.fixture
def request_info() -> RequestInformation:
    return RequestInformation(
        voice_command="send my to-do list to my phone",
        conversation_id="conv-1",
        user_id=_DEFAULT_USER_ID,
    )


@pytest.fixture
def request_info_unknown_speaker() -> RequestInformation:
    return RequestInformation(
        voice_command="send my to-do list to my phone",
        conversation_id="conv-1",
        user_id=None,
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


def _all_rows(metadata: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [row for section in metadata["sections"] for row in section["rows"]]


class TestProperties:
    def test_command_name(self, command: ExportTodoListCommand) -> None:
        assert command.command_name == "export_todo_list"

    def test_data_browser_disabled(self, command: ExportTodoListCommand) -> None:
        assert command.data_browser_mode == "disabled"

    def test_no_llm_parameters(self, command: ExportTodoListCommand) -> None:
        assert command.parameters == []

    def test_antipattern_points_to_todo_list(self, command: ExportTodoListCommand) -> None:
        assert any(ap.command_name == "todo_list" for ap in command.antipatterns)

    def test_callback_registered(self, command: ExportTodoListCommand) -> None:
        assert "save_todo_state" in command.get_callbacks()

    def test_exactly_one_primary_prompt_example(
        self, command: ExportTodoListCommand
    ) -> None:
        primaries = [e for e in command.generate_prompt_examples() if e.is_primary]
        assert len(primaries) == 1


class TestSpeakerGate:
    def test_unknown_speaker_refused(
        self, command: ExportTodoListCommand, post_mock,
        request_info_unknown_speaker: RequestInformation,
    ) -> None:
        response = command.run(request_info_unknown_speaker)
        assert not response.success
        assert "not sure who's asking" in response.context_data["message"]
        post_mock.assert_not_called()


class TestEmptyList:
    def test_empty_list_nothing_to_send(
        self, command: ExportTodoListCommand, post_mock,
        request_info: RequestInformation,
    ) -> None:
        response = command.run(request_info)
        assert response.success
        assert "nothing to send" in response.context_data["message"]
        post_mock.assert_not_called()


class TestPayloadShape:
    def test_sections_split_open_and_completed(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        _add_task(storage, "done task", completed=True)

        response = command.run(request_info)
        assert response.success
        post_mock.assert_called_once()
        metadata = post_mock.call_args.args[0]["metadata"]
        assert metadata["type"] == "interactive_list"
        assert metadata["version"] == 1
        assert metadata["command_name"] == "export_todo_list"
        assert [s["title"] for s in metadata["sections"]] == ["To do", "Completed"]
        assert [r["key"] for r in metadata["sections"][0]["rows"]] == ["open_task"]
        assert [r["key"] for r in metadata["sections"][1]["rows"]] == ["done_task"]

    def test_empty_sections_omitted(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "only open")
        command.run(request_info)
        metadata = post_mock.call_args.args[0]["metadata"]
        assert [s["title"] for s in metadata["sections"]] == ["To do"]

    def test_checkbox_control_and_default_mirrors_completed(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        _add_task(storage, "done task", completed=True)
        command.run(request_info)
        rows = {r["key"]: r for r in _all_rows(post_mock.call_args.args[0]["metadata"])}
        assert rows["open_task"]["control"] == "checkbox"
        assert rows["open_task"]["default"] == {"selected": False}
        assert rows["done_task"]["default"] == {"selected": True}

    def test_context_carries_all_shown_keys(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        _add_task(storage, "done task", completed=True)
        command.run(request_info)
        metadata = post_mock.call_args.args[0]["metadata"]
        assert set(metadata["context"]["keys"]) == {"open_task", "done_task"}

    def test_action_is_save(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        command.run(request_info)
        metadata = post_mock.call_args.args[0]["metadata"]
        assert metadata["actions"] == [
            {"label": "Save", "callback": "save_todo_state", "style": "primary"}
        ]
        assert metadata["empty_text"] == "Your to-do list is empty."

    def test_payload_envelope(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open one")
        _add_task(storage, "done one", completed=True)
        command.run(request_info)
        payload = post_mock.call_args.args[0]
        assert "2 tasks" in payload["title"]
        assert payload["category"] == "interactive_list"
        assert payload["user_id"] == _DEFAULT_USER_ID
        assert payload["create_push_notification"] is True
        assert payload["target_type"] == "user"
        assert payload["summary"]
        # Markdown checklist fallback
        assert "- [ ] open one" in payload["body"]
        assert "- [x] done one" in payload["body"]
        # node_id is injected by CC, never sent by the node
        assert "node_id" not in payload["metadata"]

    def test_rows_sorted_oldest_first(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "second", added_at="2026-06-09T13:00:00+00:00")
        _add_task(storage, "first", added_at="2026-06-09T12:00:00+00:00")
        command.run(request_info)
        rows = _all_rows(post_mock.call_args.args[0]["metadata"])
        assert [r["label"] for r in rows] == ["first", "second"]

    def test_over_cap_truncates_to_100_open_first(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        for i in range(60):
            _add_task(storage, f"open {i:03d}", added_at=f"2026-06-09T12:{i:02d}:00+00:00")
        for i in range(60):
            _add_task(storage, f"done {i:03d}", completed=True,
                      added_at=f"2026-06-09T13:{i:02d}:00+00:00")
        response = command.run(request_info)
        assert response.success
        metadata = post_mock.call_args.args[0]["metadata"]
        assert len(metadata["sections"][0]["rows"]) == 60   # all open kept
        assert len(metadata["sections"][1]["rows"]) == 40   # completed truncated
        assert sum(len(s["rows"]) for s in metadata["sections"]) == 100


class TestRunMessages:
    def test_success_message(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open one")
        _add_task(storage, "open two")
        response = command.run(request_info)
        assert response.context_data["message"] == (
            "I sent your to-do list to your phone — 2 tasks."
        )

    def test_success_message_singular(
        self, command: ExportTodoListCommand, storage: FakeStorage, post_mock,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open one")
        response = command.run(request_info)
        assert response.context_data["message"].endswith("1 task.")

    @pytest.mark.parametrize("reason", ["http_error", "no_cc_url", "import_error"])
    def test_post_failure_maps_to_spoken_error(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation, reason: str,
    ) -> None:
        _add_task(storage, "open one")
        with patch.object(
            ExportTodoListCommand, "_post_inbox_item", return_value=reason
        ):
            response = command.run(request_info)
        assert not response.success
        assert response.context_data["message"]


class TestCallback:
    def _ctx(self, *keys: str) -> Dict[str, Any]:
        return {"keys": list(keys)}

    def test_check_marks_open_task_done(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        response = command.save_todo_state(
            {"selected": [{"key": "open_task"}], "context": self._ctx("open_task")},
            request_info,
        )
        assert response.success
        record = storage.get("open_task")
        assert record["completed"] is True
        assert record["completed_at"] is not None
        assert response.context_data["completed"] == ["open task"]
        assert "marked open task done" in response.context_data["message"]

    def test_uncheck_reopens_completed_task(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "done task", completed=True,
                  completed_at="2026-06-01T00:00:00+00:00")
        # Shown but NOT in selected → reopen.
        response = command.save_todo_state(
            {"selected": [], "context": self._ctx("done_task")},
            request_info,
        )
        assert response.success
        record = storage.get("done_task")
        assert record["completed"] is False
        assert record["completed_at"] is None
        assert response.context_data["reopened"] == ["done task"]
        assert "reopened done task" in response.context_data["message"]

    def test_mixed_check_and_uncheck(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        _add_task(storage, "done task", completed=True)
        response = command.save_todo_state(
            {
                "selected": [{"key": "open_task"}],  # check open, leave done unchecked
                "context": self._ctx("open_task", "done_task"),
            },
            request_info,
        )
        assert storage.get("open_task")["completed"] is True
        assert storage.get("done_task")["completed"] is False
        assert response.context_data["completed"] == ["open task"]
        assert response.context_data["reopened"] == ["done task"]

    def test_no_change_is_idempotent(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "done task", completed=True)
        # done_task stays checked → no change.
        response = command.save_todo_state(
            {"selected": [{"key": "done_task"}], "context": self._ctx("done_task")},
            request_info,
        )
        assert response.success
        assert response.context_data["completed"] == []
        assert response.context_data["reopened"] == []
        assert "already up to date" in response.context_data["message"]

    def test_keys_outside_context_are_untouched(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """A task added after the export (not in context.keys) is never touched."""
        _add_task(storage, "shown task")
        _add_task(storage, "added later")  # open, not in context
        command.save_todo_state(
            {"selected": [{"key": "shown_task"}], "context": self._ctx("shown_task")},
            request_info,
        )
        assert storage.get("added_later")["completed"] is False

    def test_unknown_key_skipped(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """A key whose record was deleted mid-session is silently skipped."""
        response = command.save_todo_state(
            {"selected": [{"key": "ghost"}], "context": self._ctx("ghost")},
            request_info,
        )
        assert response.success
        assert response.context_data["completed"] == []

    def test_write_back_strips_meta_fields(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        command.save_todo_state(
            {"selected": [{"key": "open_task"}], "context": self._ctx("open_task")},
            request_info,
        )
        record = storage.get("open_task")
        assert "_data_key" not in record
        assert "_expires_at" not in record
        assert "data_key" not in record

    def test_legacy_no_context_reconciles_all_records(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        """Pre-context payloads: reconcile against every current todo."""
        _add_task(storage, "open task")
        _add_task(storage, "done task", completed=True)
        # No context → universe is all records. selected has open_task only.
        command.save_todo_state({"selected": [{"key": "open_task"}]}, request_info)
        assert storage.get("open_task")["completed"] is True
        assert storage.get("done_task")["completed"] is False

    def test_detail_lines_present(
        self, command: ExportTodoListCommand, storage: FakeStorage,
        request_info: RequestInformation,
    ) -> None:
        _add_task(storage, "open task")
        _add_task(storage, "done task", completed=True)
        response = command.save_todo_state(
            {
                "selected": [{"key": "open_task"}],
                "context": self._ctx("open_task", "done_task"),
            },
            request_info,
        )
        assert response.context_data["detail_lines"] == ["✓ open task", "○ done task"]

    def test_callback_always_sets_message(
        self, command: ExportTodoListCommand, request_info: RequestInformation,
    ) -> None:
        response = command.save_todo_state({"selected": []}, request_info)
        assert response.context_data.get("message")
