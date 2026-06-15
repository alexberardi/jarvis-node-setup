"""To-do list command for Jarvis.

Household to-do list: add tasks, read the list, mark tasks done or not
done, remove tasks, or clear it. Tasks persist as one record per task so
the mobile data browser renders them with a done toggle, and the
``export_todo_list`` command can push them to the phone as an interactive
checklist.

Records are intentionally household-shared: they carry no ``user_id``
field, so the command-data browser shows the same list to every household
member (``added_by`` records who added a task without scoping visibility).

Unlike the shopping list, a task is a free-form phrase, so item text is
NEVER split on "and"/commas here — "call mom and dad" is one task. The
LLM may still segment a clearly enumerated list ("walk the dog, buy milk,
and call the bank") into multiple ``tasks`` entries; the fast paths always
treat the captured phrase as a single task.
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandAntipattern,
    CommandExample,
    FastPathPattern,
    FieldSpec,
    IJarvisCommand,
    JarvisStorage,
    PreRouteResult,
    RecordSummary,
)
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation

logger = JarvisLogger(service="jarvis-node")

STORAGE_NAME = "todo_list"

_ALL_ACTIONS = ["add", "read", "complete", "uncomplete", "remove", "clear"]

_storage: JarvisStorage | None = None


def _get_storage() -> JarvisStorage:
    global _storage
    if _storage is None:
        _storage = JarvisStorage(STORAGE_NAME)
    return _storage


def _item_key(name: str) -> str:
    """Stable storage key for a task phrase ('Call the Dentist' -> 'call_the_dentist')."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _spoken_join(items: List[str]) -> str:
    """Join phrases for speech: 'X', 'X and Y', 'X, Y, and Z'."""
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


# --- Pre-route patterns ---
# Every pattern requires the literal to-do/task-list noun so these can never
# steal reminder ("remind me to call mom") or other "add X" utterances.
# Fuzzy shapes ("I need to call the dentist") fall through to the LLM.

_LIST_NOUN = r"(?:my\s+|the\s+|our\s+)?(?:to[\s-]?do|task)\s+list"

# Status words that mean "finished" vs "not finished". Kept disjoint so a
# "mark X ... not done" never matches the complete pattern.
_STATUS_DONE = r"(?:complete|completed|done|finished)"
_STATUS_UNDONE = r"(?:incomplete|unfinished|undone|not\s+(?:complete|completed|done|finished))"

_ADD_RE = re.compile(
    rf"^\s*(?:add|put)\s+(?P<task>.+?)\s+(?:to|on(?:to)?)\s+{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)

_READ_RE = re.compile(
    rf"^\s*(?:what(?:'s|\s+is)\s+on|read(?:\s+me)?|show(?:\s+me)?|list)\s+{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)

# "mark X on my todo list (as) done" | "mark X (as) done on my todo list" |
# "check/cross X off my todo list".
#
# In the "status before list" (task_m2) branch the task group must NOT absorb
# across a " not " — otherwise "mark X as NOT done on my todo list" would let
# the non-greedy group eat "X as not" and match the trailing done-word,
# stealing an uncomplete utterance. The (?:(?!\s+not\s+).)+? guard makes
# complete and uncomplete mutually exclusive on their own, independent of
# fast-path order (a user can disable one pattern but not the other). The
# "status after list" (task_m1) branch needs no guard: a done-word can never
# match "not done" after the list noun, and a real task may legitimately
# contain "not" ("mark do not disturb on my todo list as done").
_COMPLETE_RE = re.compile(
    rf"^\s*(?:"
    rf"mark\s+(?P<task_m1>.+?)\s+(?:on|in)\s+{_LIST_NOUN}\s+(?:as\s+)?{_STATUS_DONE}"
    rf"|"
    rf"mark\s+(?P<task_m2>(?:(?!\s+not\s+).)+?)\s+(?:as\s+)?{_STATUS_DONE}\s+(?:on|in)\s+{_LIST_NOUN}"
    rf"|"
    rf"(?:check(?:\s+off)?|cross(?:\s+off|\s+out)?)\s+(?P<task_c>.+?)"
    rf"\s+(?:off\s+)?(?:(?:of|from|on)\s+)?{_LIST_NOUN}"
    rf")\s*[?.!]*$",
    re.IGNORECASE,
)

# "mark X on my todo list (as) not done" | "mark X (as) not done on my todo
# list" | "uncheck X on my todo list".
_UNCOMPLETE_RE = re.compile(
    rf"^\s*(?:"
    rf"mark\s+(?P<task_m1>.+?)\s+(?:on|in)\s+{_LIST_NOUN}\s+(?:as\s+)?{_STATUS_UNDONE}"
    rf"|"
    rf"mark\s+(?P<task_m2>.+?)\s+(?:as\s+)?{_STATUS_UNDONE}\s+(?:on|in)\s+{_LIST_NOUN}"
    rf"|"
    rf"(?:uncheck|uncross)\s+(?:off\s+)?(?P<task_u>.+?)"
    rf"\s+(?:off\s+)?(?:(?:of|from|on)\s+)?{_LIST_NOUN}"
    rf")\s*[?.!]*$",
    re.IGNORECASE,
)

_REMOVE_RE = re.compile(
    rf"^\s*(?:remove|take|delete)\s+(?P<task>.+?)\s+(?:off|from)\s+(?:of\s+)?{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)

# Only "clear the whole list" is fast-pathed; "clear completed" is fuzzy
# enough (it has no list noun in the common phrasing) to leave to the LLM.
_CLEAR_RE = re.compile(
    rf"^\s*(?:clear|empty|wipe)\s+(?:out\s+)?{_LIST_NOUN}\s*[?.!]*$",
    re.IGNORECASE,
)


class TodoListCommand(IJarvisCommand):
    """Manage the household to-do list."""

    @property
    def command_name(self) -> str:
        return "todo_list"

    @property
    def description(self) -> str:
        return (
            "Manage the household to-do list: add tasks, read the list, mark "
            "tasks done or not done, remove tasks, or clear it. "
            "Use for 'add call the dentist to my to-do list', 'mark the laundry "
            "done on my to-do list', 'what's on my to-do list'. "
            "NOT for time-based reminders ('remind me to call mom at 5')."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "to do list", "todo list", "to-do list", "task list",
            "add task", "mark done", "check off task", "to do", "todo",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=True,
                description=(
                    "add=put tasks on the list, read=say what's on the list, "
                    "complete=mark tasks done, uncomplete=mark tasks not done, "
                    "remove=delete tasks, clear=delete everything"
                ),
                enum_values=_ALL_ACTIONS,
            ),
            JarvisParameter(
                "tasks", "array<string>", required=False,
                description=(
                    "Task phrases. Always a list, even for one task "
                    "(required for add, complete, uncomplete, and remove). "
                    "Each entry is a whole task — do not split a single task "
                    "on 'and' ('call mom and dad' is one task)"
                ),
            ),
            JarvisParameter(
                "scope", "string", required=False,
                description=(
                    "For clear: 'all' (default) removes every task; 'completed' "
                    "removes only the tasks already marked done"
                ),
                enum_values=["all", "completed"],
            ),
        ]

    @property
    def required_secrets(self) -> List["IJarvisSecret"]:
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "tasks is ALWAYS an array of strings, even for a single task",
            "Each task is a whole phrase — do NOT split a single task on 'and' "
            "('call mom and dad' is one task)",
            "Only split into multiple tasks when the user clearly enumerates several "
            "('add walk the dog, buy milk, and call the bank' -> three tasks)",
            "Default action is 'add' when a task is mentioned, 'read' when just asking about the list",
            "'mark X done/complete/finished' -> complete; 'mark X not done/incomplete/undone' -> uncomplete",
            "'clear my to-do list' -> clear with scope 'all'; 'clear completed/finished tasks' -> scope 'completed'",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "tasks is required for add, complete, uncomplete, and remove — if unclear, ask which task",
            "This manages a to-do list, NOT time-based reminders — 'remind me to call mom at 5' belongs to the reminder command",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="reminder",
                description=(
                    "Time-based 'remind me to call mom tomorrow' / 'remind me to "
                    "take out the trash at 8' — those are reminders, not list edits."
                ),
            ),
        ]

    # ── Mobile command-data browser ───────────────────────────────────────

    def editable_fields(self) -> List[FieldSpec]:
        return [
            # Read-only on mobile: renaming would desync the display name from
            # the storage key voice add/complete resolve against.
            FieldSpec("task", "string", label="Task", required=True, editable=False),
            FieldSpec("completed", "bool", label="Completed"),
            FieldSpec("added_by", "user_ref", label="Added by", editable=False),
            FieldSpec("added_at", "datetime", label="Added", editable=False),
            FieldSpec("completed_at", "datetime", label="Completed", editable=False),
        ]

    def display_summary(self, record: dict) -> RecordSummary:
        task = str(record.get("task") or "Task")
        completed = bool(record.get("completed"))
        title = f"✓ {task}" if completed else task
        subtitle = "Done" if completed else None
        icon = (
            "checkbox-marked-circle-outline"
            if completed
            else "checkbox-blank-circle-outline"
        )
        return RecordSummary(title=title, subtitle=subtitle, icon=icon)

    # ── Examples ──────────────────────────────────────────────────────

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                "Add call the dentist to my to-do list",
                {"action": "add", "tasks": ["call the dentist"]},
                is_primary=True,
            ),
            CommandExample(
                "Put fold the laundry on my to-do list",
                {"action": "add", "tasks": ["fold the laundry"]},
            ),
            CommandExample(
                "What's on my to-do list?",
                {"action": "read"},
            ),
            CommandExample(
                "Mark the laundry done on my to-do list",
                {"action": "complete", "tasks": ["the laundry"]},
            ),
            CommandExample(
                "Mark call the dentist as not done on my to-do list",
                {"action": "uncomplete", "tasks": ["call the dentist"]},
            ),
            CommandExample(
                "Remove fold the laundry from my to-do list",
                {"action": "remove", "tasks": ["fold the laundry"]},
            ),
            CommandExample(
                "Add walk the dog, buy milk, and call the bank to my to-do list",
                {"action": "add", "tasks": ["walk the dog", "buy milk", "call the bank"]},
            ),
            CommandExample(
                "Clear my to-do list",
                {"action": "clear"},
            ),
            CommandExample(
                "Clear the completed tasks on my to-do list",
                {"action": "clear", "scope": "completed"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items: list[tuple[str, dict[str, Any], bool]] = [
            # Add — single
            ("Add call the dentist to my to-do list", {"action": "add", "tasks": ["call the dentist"]}, True),
            ("Put fold the laundry on the to-do list", {"action": "add", "tasks": ["fold the laundry"]}, False),
            ("Add take out the trash to my todo list", {"action": "add", "tasks": ["take out the trash"]}, False),
            ("Can you add water the plants to the task list", {"action": "add", "tasks": ["water the plants"]}, False),
            ("Add call mom and dad to my to-do list", {"action": "add", "tasks": ["call mom and dad"]}, False),
            # Add — compound (clearly enumerated)
            ("Add walk the dog, buy milk, and call the bank to my to-do list", {"action": "add", "tasks": ["walk the dog", "buy milk", "call the bank"]}, False),
            ("Put mow the lawn and wash the car on my to-do list", {"action": "add", "tasks": ["mow the lawn", "wash the car"]}, False),
            # Read
            ("What's on my to-do list?", {"action": "read"}, False),
            ("Read me my to-do list", {"action": "read"}, False),
            ("Show me my to-do list", {"action": "read"}, False),
            ("What do I have to do today?", {"action": "read"}, False),
            # Complete
            ("Mark the laundry done on my to-do list", {"action": "complete", "tasks": ["the laundry"]}, False),
            ("Check off call the dentist on my to-do list", {"action": "complete", "tasks": ["call the dentist"]}, False),
            ("Mark take out the trash as complete on my to-do list", {"action": "complete", "tasks": ["take out the trash"]}, False),
            ("Cross water the plants off my to-do list", {"action": "complete", "tasks": ["water the plants"]}, False),
            # Uncomplete
            ("Mark the laundry as not done on my to-do list", {"action": "uncomplete", "tasks": ["the laundry"]}, False),
            ("Mark call the dentist incomplete on my to-do list", {"action": "uncomplete", "tasks": ["call the dentist"]}, False),
            ("Uncheck mow the lawn on my to-do list", {"action": "uncomplete", "tasks": ["mow the lawn"]}, False),
            # Remove
            ("Remove fold the laundry from my to-do list", {"action": "remove", "tasks": ["fold the laundry"]}, False),
            ("Take wash the car off my to-do list", {"action": "remove", "tasks": ["wash the car"]}, False),
            # Clear
            ("Clear my to-do list", {"action": "clear"}, False),
            ("Empty my task list", {"action": "clear"}, False),
            ("Clear the completed tasks on my to-do list", {"action": "clear", "scope": "completed"}, False),
        ]
        return [
            CommandExample(voice, params, is_primary)
            for voice, params, is_primary in items
        ]

    # ── Pre-route ─────────────────────────────────────────────────────
    # Every verb is deterministic when the utterance names the to-do/task
    # list explicitly, so all six actions get fast-paths. Fuzzy shapes
    # ("I keep forgetting to call the dentist") fall through to the LLM.

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="todo_list.add",
                description="Bypass LLM for 'add X to the to-do list'",
                example="add call the dentist to my to-do list",
                regex=_ADD_RE.pattern,
                handler="_fp_add",
            ),
            FastPathPattern(
                id="todo_list.read",
                description="Bypass LLM for 'what's on the to-do list'",
                example="what's on my to-do list",
                regex=_READ_RE.pattern,
                handler="_fp_read",
            ),
            # Uncomplete is tried BEFORE complete: a "not done"/"not finished"
            # phrasing contains a done-word ("finished"), and complete's
            # non-greedy task group would otherwise absorb the "not" and match
            # on the trailing done-word. Letting the negation-aware pattern win
            # first keeps "mark X as not finished" out of the complete path.
            FastPathPattern(
                id="todo_list.uncomplete",
                description="Bypass LLM for 'mark X not done on the to-do list'",
                example="mark the laundry as not done on my to-do list",
                regex=_UNCOMPLETE_RE.pattern,
                handler="_fp_uncomplete",
            ),
            FastPathPattern(
                id="todo_list.complete",
                description="Bypass LLM for 'mark X done on the to-do list'",
                example="mark the laundry done on my to-do list",
                regex=_COMPLETE_RE.pattern,
                handler="_fp_complete",
            ),
            FastPathPattern(
                id="todo_list.remove",
                description="Bypass LLM for 'remove X from the to-do list'",
                example="remove fold the laundry from my to-do list",
                regex=_REMOVE_RE.pattern,
                handler="_fp_remove",
            ),
            FastPathPattern(
                id="todo_list.clear",
                description="Bypass LLM for 'clear the to-do list'",
                example="clear my to-do list",
                regex=_CLEAR_RE.pattern,
                handler="_fp_clear",
            ),
        ]

    def _fp_add(self, match, voice_command: str) -> PreRouteResult | None:
        task = (match.group("task") or "").strip()
        if not task:
            return None
        return PreRouteResult(arguments={"action": "add", "tasks": [task]})

    def _fp_read(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "read"})

    def _fp_complete(self, match, voice_command: str) -> PreRouteResult | None:
        task = (
            match.group("task_m1") or match.group("task_m2") or match.group("task_c") or ""
        ).strip()
        if not task:
            return None
        return PreRouteResult(arguments={"action": "complete", "tasks": [task]})

    def _fp_uncomplete(self, match, voice_command: str) -> PreRouteResult | None:
        task = (
            match.group("task_m1") or match.group("task_m2") or match.group("task_u") or ""
        ).strip()
        if not task:
            return None
        return PreRouteResult(arguments={"action": "uncomplete", "tasks": [task]})

    def _fp_remove(self, match, voice_command: str) -> PreRouteResult | None:
        task = (match.group("task") or "").strip()
        if not task:
            return None
        return PreRouteResult(arguments={"action": "remove", "tasks": [task]})

    def _fp_clear(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "clear"})

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        tasks = args.get("tasks")
        if isinstance(tasks, str):
            cleaned = tasks.strip()
            args["tasks"] = [cleaned] if cleaned else []
        elif isinstance(tasks, list):
            # Respect the LLM's segmentation — never re-split entries, which
            # would shred a single task containing "and" ("call mom and dad").
            args["tasks"] = [str(entry).strip() for entry in tasks if str(entry).strip()]
        if not args.get("action"):
            args["action"] = "add" if args.get("tasks") else "read"
        scope = args.get("scope")
        if scope is not None and scope not in ("all", "completed"):
            args.pop("scope", None)
        return args

    # ── Main execution ────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str = kwargs.get("action", "read")
        raw_tasks = kwargs.get("tasks") or []
        tasks = [str(t).strip() for t in raw_tasks if str(t).strip()]

        if action in ("add", "complete", "uncomplete", "remove") and not tasks:
            question = "Which task?"
            return CommandResponse.error_response(
                error_details=question,
                context_data={"error": "missing_tasks", "message": question},
                wait_for_input=True,
            )

        added_by = request_info.user_id if request_info is not None else None

        if action == "add":
            return self._run_add(tasks, added_by)
        elif action == "read":
            return self._run_read()
        elif action == "complete":
            return self._run_set_completed(tasks, completed=True)
        elif action == "uncomplete":
            return self._run_set_completed(tasks, completed=False)
        elif action == "remove":
            return self._run_remove(tasks)
        elif action == "clear":
            return self._run_clear(scope=kwargs.get("scope") or "all")
        else:
            return CommandResponse.error_response(
                error_details=f"Unknown to-do list action: {action}",
                context_data={"message": f"I don't know the to-do list action '{action}'."},
            )

    # ── Add ───────────────────────────────────────────────────────────

    def _run_add(self, tasks: List[str], added_by: int | None) -> CommandResponse:
        storage = _get_storage()
        now = datetime.now(timezone.utc).isoformat()

        added: List[str] = []
        already: List[str] = []
        for task in tasks:
            key = _item_key(task)
            if not key:
                continue
            existing = storage.get(key)
            if existing and not existing.get("completed"):
                # Already open on the list — no-op.
                already.append(existing.get("task") or task)
                continue
            # New, or re-adding a completed task (which reopens it).
            record: Dict[str, Any] = {
                "task": task,
                "completed": False,
                "added_at": now,
                "completed_at": None,
            }
            if added_by is not None:
                record["added_by"] = added_by
            storage.save(key, record)
            added.append(task)

        sentences: List[str] = []
        if added:
            sentences.append(f"Added {_spoken_join(added)} to your to-do list.")
        if already:
            sentences.append(
                f"{_spoken_join(already).capitalize()} "
                f"{'is' if len(already) == 1 else 'are'} already on it."
            )
        if not sentences:
            return CommandResponse.error_response(
                error_details="I couldn't make out any tasks to add.",
                context_data={"message": "I couldn't make out any tasks to add."},
            )

        message = " ".join(sentences)
        logger.info(f"To-do list add: {added} (already present: {already})")
        return CommandResponse.success_response(
            context_data={
                "message": message,
                "added": added,
                "already_on_list": already,
            },
            wait_for_input=False,
        )

    # ── Read ──────────────────────────────────────────────────────────

    def _run_read(self) -> CommandResponse:
        records = _get_storage().get_all()
        open_records = [r for r in records if r.get("task") and not r.get("completed")]
        open_records.sort(key=lambda r: str(r.get("added_at") or ""))
        open_tasks = [str(r.get("task")) for r in open_records]

        if not open_tasks:
            message = "Your to-do list is empty."
        elif len(open_tasks) == 1:
            message = f"You have one thing on your to-do list: {open_tasks[0]}."
        else:
            message = (
                f"You have {len(open_tasks)} things on your to-do list: "
                f"{_spoken_join(open_tasks)}."
            )

        return CommandResponse.success_response(
            context_data={"message": message, "tasks": open_tasks},
            wait_for_input=False,
        )

    # ── Complete / uncomplete / remove ────────────────────────────────

    @staticmethod
    def _normalize_record(record: Dict[str, Any], key: str) -> Dict[str, Any]:
        """Strip repository meta fields and attach the real storage key."""
        record.pop("_data_key", None)
        record.pop("_expires_at", None)
        record["data_key"] = key
        return record

    def _find_record(self, task: str) -> Dict[str, Any] | None:
        """Find a record by exact key, falling back to substring match.

        The fallback iterates in key order so a fuzzy match is deterministic
        when several tasks contain the needle.
        """
        storage = _get_storage()
        key = _item_key(task)
        exact = storage.get(key)
        if exact is not None:
            return self._normalize_record(exact, key)
        needle = task.lower().strip()
        if not needle:
            return None
        candidates = sorted(storage.get_all(), key=lambda r: str(r.get("_data_key", "")))
        for record in candidates:
            name = str(record.get("task") or "").lower()
            if needle in name or name in needle:
                return self._normalize_record(
                    record, str(record.get("_data_key") or _item_key(name))
                )
        return None

    def _run_set_completed(self, tasks: List[str], *, completed: bool) -> CommandResponse:
        storage = _get_storage()
        now = datetime.now(timezone.utc).isoformat()

        changed: List[str] = []
        missing: List[str] = []
        for task in tasks:
            record = self._find_record(task)
            if record is None:
                missing.append(task)
                continue
            record["completed"] = completed
            record["completed_at"] = now if completed else None
            key = record.pop("data_key", None) or _item_key(task)
            storage.save(key, record)
            changed.append(str(record.get("task") or task))

        verb = "done" if completed else "not done"
        if changed and missing:
            message = (
                f"Marked {_spoken_join(changed)} {verb}. "
                f"I couldn't find {_spoken_join(missing)} on the list."
            )
        elif changed:
            message = f"Marked {_spoken_join(changed)} {verb}."
        else:
            message = f"I couldn't find {_spoken_join(missing)} on your to-do list."

        result_key = "completed" if completed else "reopened"
        return CommandResponse.success_response(
            context_data={"message": message, result_key: changed, "not_found": missing},
            wait_for_input=False,
        )

    def _run_remove(self, tasks: List[str]) -> CommandResponse:
        storage = _get_storage()

        removed: List[str] = []
        missing: List[str] = []
        for task in tasks:
            record = self._find_record(task)
            if record is None:
                missing.append(task)
                continue
            key = record.get("data_key") or _item_key(task)
            if storage.delete(key):
                removed.append(str(record.get("task") or task))
            else:
                missing.append(task)

        if removed and missing:
            message = (
                f"Removed {_spoken_join(removed)} from your to-do list. "
                f"I couldn't find {_spoken_join(missing)}."
            )
        elif removed:
            message = f"Removed {_spoken_join(removed)} from your to-do list."
        else:
            message = f"I couldn't find {_spoken_join(missing)} on your to-do list."

        return CommandResponse.success_response(
            context_data={"message": message, "removed": removed, "not_found": missing},
            wait_for_input=False,
        )

    # ── Clear ─────────────────────────────────────────────────────────

    def _run_clear(self, scope: str = "all") -> CommandResponse:
        storage = _get_storage()

        if scope == "completed":
            removed = 0
            for raw in storage.get_all():
                key = str(raw.get("_data_key") or "")
                if not key:
                    continue
                if raw.get("completed"):
                    storage.delete(key)
                    removed += 1
            if removed == 0:
                message = "You have no completed tasks to clear."
            else:
                noun = "one completed task" if removed == 1 else f"{removed} completed tasks"
                message = f"Cleared {noun} from your to-do list."
            logger.info(f"To-do list cleared (completed): {removed} tasks removed")
            return CommandResponse.success_response(
                context_data={"message": message, "removed_count": removed, "scope": scope},
                wait_for_input=False,
            )

        count = storage.delete_all()
        if count == 0:
            message = "Your to-do list is already empty."
        else:
            noun = "one task" if count == 1 else f"{count} tasks"
            message = f"Cleared your to-do list — removed {noun}."
        logger.info(f"To-do list cleared (all): {count} tasks removed")
        return CommandResponse.success_response(
            context_data={"message": message, "removed_count": count, "scope": "all"},
            wait_for_input=False,
        )
