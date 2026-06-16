"""Export to-do list command for Jarvis.

Sends the household to-do list to the speaker's phone as an
interactive-checklist inbox item with a push notification. The mobile
app's generic interactive-list screen renders each task as a checkbox —
completed tasks arrive pre-checked, open tasks unchecked — and the user
toggles freely, then taps "Save". That fires the ``save_todo_state``
callback on this command, which reconciles the checked/unchecked state
back onto the live ``todo_list`` records: checked → completed, unchecked
→ reopened. Tasks the user didn't see are never touched.

The interactive-list contract is batch-only: a toggle updates local state
and the callback fires once on Save with the full set of checked rows
(``selected``). To know which shown tasks were UNchecked, the payload
carries every shown key in ``context["keys"]`` — anything in that set but
not in ``selected`` is reconciled to "open".

Known platform edge: the mobile Save button is disabled when zero rows are
checked, so "reopen every task at once" can't be saved from this screen.
Use the voice command ("mark X not done on my to-do list") for that — the
callback below still handles an empty selection correctly if it ever
arrives.

Records are household-shared (no ``user_id`` field); this command only
reads/writes the same ``todo_list`` storage the TodoListCommand owns, so it
exposes no data of its own to the mobile browser (``data_browser_mode`` is
``disabled``).
"""

import re
from datetime import datetime, timezone
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandAntipattern,
    CommandExample,
    DataBrowserMode,
    FastPathPattern,
    IJarvisCommand,
    InteractiveAction,
    InteractiveList,
    InteractiveRow,
    InteractiveSection,
    JarvisStorage,
    PreRouteResult,
    callback,
)
from commands.todo_list_command import _item_key, _spoken_join
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation

logger = JarvisLogger(service="jarvis-node")

STORAGE_NAME = "todo_list"

# Wired into the interactive-list payload's action button — mobile fires
# this @callback by name. The inbox category is InteractiveList.CATEGORY
# ("interactive_list"), routing the item to the generic mobile renderer.
CALLBACK_NAME = "save_todo_state"

# Interactive-list contract cap (SDK _MAX_TOTAL_ROWS): payloads over this
# are rejected at construction, so the producer truncates instead.
_MAX_PAYLOAD_ROWS = 100

_storage: JarvisStorage | None = None


def _get_storage() -> JarvisStorage:
    global _storage
    if _storage is None:
        _storage = JarvisStorage(STORAGE_NAME)
    return _storage


# ── Pre-route pattern ──────────────────────────────────────────────────────
# Anchored both ends. "send" / "export" are unambiguous (the manage command's
# read verbs are what/read/show/list, never send/export). A bare "show me my
# to-do list" stays a READ (spoken aloud); only an explicit "...on my phone"
# tail turns show/pull-up/open into an export.

_LIST_NOUN = r"(?:my\s+|the\s+|our\s+)?(?:to[\s-]?do|task)\s+list"

_EXPORT_RE = re.compile(
    r"^\s*(?:"
    rf"(?:send|export)\s+(?:me\s+)?{_LIST_NOUN}(?:\s+(?:to|on)\s+(?:my\s+)?phone)?"
    r"|"
    rf"(?:show|pull\s+up|open|bring\s+up)\s+(?:me\s+)?{_LIST_NOUN}\s+(?:to|on)\s+(?:my\s+)?phone"
    r")\s*[?.!]*$",
    re.IGNORECASE,
)


class ExportTodoListCommand(IJarvisCommand):
    """Send the to-do list to the phone as an interactive, check-off-able checklist."""

    @property
    def command_name(self) -> str:
        return "export_todo_list"

    @property
    def data_browser_mode(self) -> DataBrowserMode:
        # This command owns no storage of its own — it reads/writes the
        # todo_list records (browsed under todo_list). Hiding it keeps the
        # generic data browser from showing an empty duplicate section.
        return "disabled"

    @property
    def description(self) -> str:
        return (
            "Send the to-do list to your phone as an interactive checklist you "
            "can check off and tap Save to sync back. Use for 'send my to-do "
            "list to my phone', 'export the to-do list'."
        )

    @property
    def keywords(self) -> List[str]:
        return ["export", "send", "to-do list", "todo list", "task list", "checklist", "phone"]

    @property
    def parameters(self) -> List[JarvisParameter]:
        # Deliberately empty — "send my to-do list" carries no extracted
        # values. The destination is always the speaker's phone.
        return []

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="todo_list",
                description=(
                    "Adding/reading/completing tasks is the todo_list command — "
                    "this one only sends the list to your phone as a checklist."
                ),
            ),
        ]

    # ── Examples ──────────────────────────────────────────────────────────

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample("Send my to-do list to my phone", {}, is_primary=True),
            CommandExample("Export the to-do list", {}),
            CommandExample("Send me my task list", {}),
            CommandExample("Show me my to-do list on my phone", {}),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        phrases: List[tuple[str, Dict[str, Any], bool]] = [
            ("Send my to-do list to my phone", {}, True),
            ("Export the to-do list", {}, False),
            ("Send me my to-do list", {}, False),
            ("Export my task list", {}, False),
            ("Send our to-do list to my phone", {}, False),
            ("Send the to-do list", {}, False),
            ("Export my to-do list to my phone", {}, False),
            ("Show me my to-do list on my phone", {}, False),
            ("Pull up my to-do list on my phone", {}, False),
            ("Send my task list to the phone", {}, False),
        ]
        return [
            CommandExample(voice, args, is_primary)
            for voice, args, is_primary in phrases
        ]

    # ── Pre-route ─────────────────────────────────────────────────────────

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="export_todo_list.export",
                description="Bypass LLM for 'send/export the to-do list (to my phone)'",
                example="send my to-do list to my phone",
                regex=_EXPORT_RE.pattern,
                handler="_fp_export",
            ),
        ]

    def _fp_export(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={})

    # ── Main execution ────────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        speaker_user_id = request_info.user_id if request_info is not None else None
        if speaker_user_id is None:
            message = (
                "I'm not sure who's asking — I need to know whose phone to "
                "send the list to. Try training your voice in the app."
            )
            logger.warning("export_todo_list refused: no speaker_user_id")
            return CommandResponse.error_response(
                error_details="Unknown speaker — cannot target the export push.",
                context_data={"error": "unknown_speaker", "message": message},
            )

        records = [r for r in _get_storage().get_all() if r.get("task")]
        if not records:
            message = "Your to-do list is empty — nothing to send."
            return CommandResponse.success_response(
                context_data={"message": message, "item_count": 0},
                wait_for_input=False,
            )

        open_take, done_take = self._collect_entries(records)
        shown = open_take + done_take
        item_count = len(shown)
        open_count = len(open_take)
        done_count = len(done_take)

        title = f"To-do list — {item_count} {'task' if item_count == 1 else 'tasks'}"
        payload = {
            "title": title,
            "summary": self._build_summary(open_count, done_count),
            "body": self._build_body(open_take, done_take),
            "category": InteractiveList.CATEGORY,
            "metadata": self._build_metadata(open_take, done_take),
            "user_id": speaker_user_id,
            "create_push_notification": True,
            "target_type": "user",
        }

        post_result = self._post_inbox_item(payload)
        if post_result != "ok":
            return self._post_failure_response(post_result, speaker_user_id)

        message = (
            f"I sent your to-do list to your phone — {item_count} "
            f"{'task' if item_count == 1 else 'tasks'}."
        )
        logger.info(
            "export_todo_list sent",
            user_id=speaker_user_id,
            item_count=item_count,
            open_count=open_count,
            done_count=done_count,
        )
        return CommandResponse.success_response(
            context_data={
                "message": message,
                "item_count": item_count,
                "open_count": open_count,
                "done_count": done_count,
            },
            wait_for_input=False,
        )

    # ── Payload helpers ───────────────────────────────────────────────────

    def _collect_entries(
        self, records: List[Dict[str, Any]]
    ) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Split open vs completed tasks, sorted oldest-first, capped to 100.

        Open tasks come first in document order, so on overflow the completed
        tail is what gets dropped — open work is never hidden. Overflow tasks
        stay on the list and ride along on the next export.
        """
        open_entries: List[Dict[str, Any]] = []
        done_entries: List[Dict[str, Any]] = []
        for record in records:
            entry = {
                "key": str(record.get("_data_key") or _item_key(str(record.get("task")))),
                "task": str(record.get("task")),
                "completed": bool(record.get("completed")),
                "added_at": str(record.get("added_at") or ""),
            }
            (done_entries if entry["completed"] else open_entries).append(entry)

        open_entries.sort(key=lambda e: e["added_at"])
        done_entries.sort(key=lambda e: e["added_at"])

        open_take = open_entries[:_MAX_PAYLOAD_ROWS]
        done_take = done_entries[: max(0, _MAX_PAYLOAD_ROWS - len(open_take))]
        return open_take, done_take

    def _build_metadata(
        self, open_take: List[Dict[str, Any]], done_take: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Interactive-list v1 payload: each task a checkbox row.

        ``default_selected`` mirrors ``completed`` (checked == done), so the
        list opens already reflecting the saved state. Only non-empty sections
        ship. The full shown key-set rides in ``context["keys"]`` so the
        callback can reconcile UNchecked rows (which never appear in
        ``selected``) back to open.
        """
        sections: List[InteractiveSection] = []
        for title, entries in (("To do", open_take), ("Completed", done_take)):
            rows = [
                InteractiveRow(
                    # Labels are raw user speech — slice to the SDK cap (120)
                    # since the builders reject over-long labels.
                    key=entry["key"],
                    label=entry["task"][:120],
                    control="checkbox",
                    default_selected=entry["completed"],
                )
                for entry in entries
            ]
            if rows:
                sections.append(InteractiveSection(title=title, rows=rows))
        if not sections:
            # run() short-circuits on an empty list before building this, but
            # the contract for an empty payload is one untitled zero-row
            # section so mobile renders empty_text.
            sections = [InteractiveSection(rows=[])]

        shown_keys = [e["key"] for e in open_take + done_take]
        return InteractiveList(
            command_name=self.command_name,
            sections=sections,
            actions=[
                InteractiveAction(label="Save", callback=CALLBACK_NAME, style="primary")
            ],
            context={"keys": shown_keys},
            empty_text="Your to-do list is empty.",
        ).to_dict()

    @staticmethod
    def _build_summary(open_count: int, done_count: int) -> str:
        open_part = f"{open_count} to do"
        done_part = f"{done_count} done"
        return f"{open_part}, {done_part}. Check off what you've finished and tap Save."

    @staticmethod
    def _build_body(
        open_take: List[Dict[str, Any]], done_take: List[Dict[str, Any]]
    ) -> str:
        """Markdown checklist fallback for clients without the rich renderer."""
        lines = [f"- [ ] {e['task']}" for e in open_take]
        lines += [f"- [x] {e['task']}" for e in done_take]
        return "\n".join(lines)

    @staticmethod
    def _post_inbox_item(payload: Dict[str, Any]) -> str:
        """Return ``"ok"`` on success, or a short failure reason string.

        Discriminated tags (not bool) so run() can map each failure mode to a
        distinct voice response.
        """
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url
        except ImportError as e:
            logger.error("export_todo_list import failed", error=str(e))
            return "import_error"

        cc_url = get_command_center_url()
        if not cc_url:
            logger.error("export_todo_list: get_command_center_url returned empty")
            return "no_cc_url"

        result = RestClient.post(
            f"{cc_url}/api/v0/node/inbox-item",
            data=payload,
            timeout=5,
        )
        if result is None:
            return "http_error"
        return "ok"

    def _post_failure_response(self, post_result: str, user_id: int) -> CommandResponse:
        if post_result == "no_cc_url":
            message = "I can't find the server to send your to-do list."
            details = "service_discovery returned no command-center URL"
        elif post_result == "import_error":
            message = "I can't export the list — the send module isn't loaded."
            details = (
                "ImportError in _post_inbox_item — clients.rest_client or "
                "utils.service_discovery missing"
            )
        else:
            message = "I couldn't reach the server right now."
            details = "RestClient.post returned None (HTTP error, timeout, or unreachable)"

        logger.error("export_todo_list failed", reason=post_result, user_id=user_id)
        return CommandResponse.error_response(
            error_details=details,
            context_data={"message": message},
        )

    # ── Callback: mobile saved the checklist, reconcile completion state ──

    @callback(CALLBACK_NAME)
    def save_todo_state(
        self, data: dict, request_info: RequestInformation
    ) -> CommandResponse:
        # {action, selected: [{key}], context: {keys: [...]}} — `selected` is
        # the rows the user left CHECKED (= done). `context.keys` is every row
        # we shipped, so any shipped key NOT in selected was unchecked (= open).
        # checkbox rows carry no quantity, so we only read keys.
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        raw_keys = context.get("keys")
        shown_keys: List[str] | None = None
        if isinstance(raw_keys, list):
            shown_keys = [str(k).strip() for k in raw_keys if str(k).strip()]

        completed_keys: set[str] = set()
        for entry in data.get("selected") or []:
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("key") or "").strip()
            if key:
                completed_keys.add(key)

        storage = _get_storage()

        # The universe to reconcile: the keys we shipped (preferred), or — for
        # a legacy payload with no context — every current todo. Either way,
        # only keys whose record still exists are touched, so a task the user
        # deleted by voice mid-session is silently skipped.
        if shown_keys is not None:
            universe = shown_keys
        else:
            universe = [
                str(r.get("_data_key") or "")
                for r in storage.get_all()
                if r.get("_data_key")
            ]

        now = datetime.now(timezone.utc).isoformat()
        newly_done: List[str] = []
        reopened: List[str] = []
        seen: set[str] = set()
        for key in universe:
            if not key or key in seen:
                continue
            seen.add(key)
            record = storage.get(key)
            if record is None:
                continue
            should_be_completed = key in completed_keys
            if bool(record.get("completed")) == should_be_completed:
                continue
            # Meta-field hygiene: never persist repository meta fields.
            record.pop("_data_key", None)
            record.pop("_expires_at", None)
            record.pop("data_key", None)
            record["completed"] = should_be_completed
            record["completed_at"] = now if should_be_completed else None
            storage.save(key, record)
            task_name = str(record.get("task") or key)
            (newly_done if should_be_completed else reopened).append(task_name)

        parts: List[str] = []
        if newly_done:
            parts.append(f"marked {_spoken_join(newly_done)} done")
        if reopened:
            parts.append(f"reopened {_spoken_join(reopened)}")
        if parts:
            message = "Updated your to-do list — " + " and ".join(parts) + "."
        else:
            message = "Your to-do list is already up to date."

        logger.info(
            "export_todo_list save_todo_state",
            completed=len(newly_done),
            reopened=len(reopened),
            selected_count=len(completed_keys),
        )
        return CommandResponse.success_response(
            context_data={
                "message": message,
                # detail_lines renders as a checkmarked list on the generic
                # result view.
                "detail_lines": [f"✓ {t}" for t in newly_done]
                + [f"○ {t}" for t in reopened],
                "completed": newly_done,
                "reopened": reopened,
            },
            wait_for_input=False,
        )
