"""Reminder command for Jarvis.

Unified command for setting, listing, deleting, and snoozing reminders.
Reminders persist across restarts and support recurrence (daily, weekly,
weekdays, monthly).
"""

import re
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandAntipattern,
    CommandExample,
    FastPathPattern,
    FieldSpec,
    IJarvisCommand,
    PreRouteResult,
    RecordSummary,
)
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from services.reminder_service import (
    get_reminder_service,
    has_explicit_time,
    ReminderService,
)

logger = JarvisLogger(service="jarvis-node")

_ALL_ACTIONS = ["set", "list", "delete", "snooze"]

_RECURRENCE_VALUES = ["daily", "weekdays", "weekly", "biweekly", "monthly"]


# --- Pre-route patterns ---
# Only the deterministic shapes get fast-paths. "Set" reminders need date
# parsing the LLM is much better at, so they fall through. The shapes here
# are unambiguous about action + (optional) numeric duration.

_LIST_RE = re.compile(
    r"^\s*(?:"
    r"what\s+reminders?\s+do\s+i\s+have"
    r"|list\s+(?:my\s+)?reminders?"
    r"|show\s+(?:me\s+)?(?:my\s+)?reminders?"
    r"|do\s+i\s+have\s+any\s+(?:upcoming\s+)?reminders?"
    r"|any\s+reminders?(?:\s+(?:for|today))?"
    r")\s*[?.!]*$",
    re.IGNORECASE,
)

_LIST_TODAY_RE = re.compile(
    r"^\s*(?:"
    r"(?:any|what)\s+reminders?\s+(?:for|do\s+i\s+have)\s+today"
    r"|reminders?\s+(?:for\s+)?today"
    r"|do\s+i\s+have\s+(?:any\s+)?reminders?\s+today"
    r")\s*[?.!]*$",
    re.IGNORECASE,
)

_LIST_RECURRING_RE = re.compile(
    r"^\s*(?:"
    r"show\s+(?:me\s+)?my\s+recurring\s+reminders?"
    r"|(?:list\s+)?(?:my\s+)?recurring\s+reminders?"
    r")\s*[?.!]*$",
    re.IGNORECASE,
)

_DELETE_ALL_RE = re.compile(
    r"^\s*(?:delete|cancel|clear|remove)\s+all\s+(?:my\s+|of\s+my\s+)?reminders?\s*[?.!]*$",
    re.IGNORECASE,
)

# "Snooze" alone → use defaults (10 min, most recent). "Snooze for N minutes" /
# "snooze for N hours" → set explicit minutes. Avoid claiming "snooze the X
# reminder for N minutes" — the labelled form needs LLM fuzzy matching.
_SNOOZE_BARE_RE = re.compile(
    r"^\s*snooze(?:\s+(?:that|it|this|the\s+reminder))?\s*[?.!]*$",
    re.IGNORECASE,
)

_SNOOZE_FOR_RE = re.compile(
    r"^\s*snooze(?:\s+(?:that|it|this))?\s+for\s+"
    r"(?P<n>\d+)\s+"
    r"(?P<unit>minute|minutes|min|mins|hour|hours|hr|hrs)"
    r"\s*[?.!]*$",
    re.IGNORECASE,
)


class ReminderCommand(IJarvisCommand):
    """Manage reminders: set, list, delete, and snooze."""

    @property
    def command_name(self) -> str:
        return "reminder"

    @property
    def description(self) -> str:
        return (
            "Set, list, delete, or snooze reminders. "
            "Use for 'remind me to...', 'what reminders do I have', 'cancel/delete reminder', 'snooze'. "
            "NOT for timers or countdowns — use set_timer for those."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "remind", "reminder", "reminders", "remind me",
            "don't forget", "remember to", "snooze",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=True,
                description="set=create reminder, list=show reminders, delete=cancel, snooze=delay",
                enum_values=_ALL_ACTIONS,
            ),
            JarvisParameter(
                "text", "string", required=False,
                description="What to be reminded about (required for 'set', optional for 'delete'/'snooze' as fuzzy match)",
            ),
            JarvisParameter(
                "resolved_datetimes", "array<datetime>", required=False,
                description="Date keys like 'today', 'tomorrow', 'next_monday', 'morning', 'tomorrow_evening'. Server resolves to dates.",
            ),
            JarvisParameter(
                "time", "string", required=False,
                description="Explicit time in HH:MM 24h format (e.g., '15:00' for 3 PM)",
            ),
            JarvisParameter(
                "relative_minutes", "int", required=False,
                description="Minutes from now (e.g., 30 for 'in 30 minutes', 120 for 'in 2 hours')",
            ),
            JarvisParameter(
                "recurrence", "string", required=False,
                description="Repeat schedule",
                enum_values=_RECURRENCE_VALUES,
            ),
            JarvisParameter(
                "filter", "string", required=False,
                description="For list action: 'all' (default), 'today', 'recurring'",
                enum_values=["all", "today", "recurring"],
            ),
            JarvisParameter(
                "scope", "string", required=False,
                description="For delete action: 'one' (default) or 'all'",
                enum_values=["one", "all"],
            ),
            JarvisParameter(
                "minutes", "int", required=False,
                description="Snooze duration in minutes (default: 10)",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        from core.ijarvis_secret import JarvisSecret
        return [
            JarvisSecret(
                "REMINDER_PUSH_NOTIFICATIONS",
                "Also send reminders as push notifications to your phone",
                "integration",
                "bool",
                required=False,
                is_sensitive=False,
                friendly_name="Push Notifications",
            ),
        ]

    @property
    def rules(self) -> List[str]:
        return [
            "Default action is 'set' when user says 'remind me...'",
            "For 'in 30 minutes' or 'in 2 hours', use relative_minutes (30, 120)",
            "For 'at 3 PM', use time='15:00'",
            "For 'tomorrow at 3 PM', use resolved_datetimes=['tomorrow'] + time='15:00'",
            "For 'every day at 8 AM', use time='08:00' + recurrence='daily'",
            "For 'every Monday', use resolved_datetimes=['next_monday'] + recurrence='weekly'",
            "For 'every other Sunday', use resolved_datetimes=['next_sunday'] + recurrence='biweekly'",
            "The text parameter should capture WHAT to be reminded about, not WHEN",
            "'snooze' with no target snoozes the most recently fired reminder",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "text is ALWAYS required for action='set' — if unclear, ask 'What should I remind you about?'",
            "At least one time parameter (resolved_datetimes with a time-of-day key, time, or relative_minutes) is required for 'set'",
            "This is for reminders (absolute time), NOT timers (relative duration countdowns)",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="set_timer",
                description="Short relative-duration countdowns: 'set a timer for 5 minutes', 'timer for 30 seconds', 'countdown from 10', 'wake me up in 5 minutes'.",
            ),
        ]

    # ── Mobile command-data browser ───────────────────────────────────────

    @property
    def data_browser_storage_name(self) -> str:
        # Historical: records persist under "set_reminder", not "reminder".
        from services.reminder_service import COMMAND_NAME
        return COMMAND_NAME

    def editable_fields(self) -> List[FieldSpec]:
        return [
            FieldSpec("reminder_id", "id", label="ID", editable=False),
            FieldSpec("text", "string", label="What", required=True),
            FieldSpec("due_at", "datetime", label="When", required=True),
            FieldSpec(
                "recurrence",
                "enum",
                label="Repeat",
                enum_values=_RECURRENCE_VALUES,
            ),
            FieldSpec("snooze_until", "datetime", label="Snoozed until", editable=False),
            FieldSpec("user_id", "user_ref", label="Owner", editable=False),
            FieldSpec("announced", "bool", label="Fired", editable=False),
            FieldSpec("announce_count", "int", label="Times fired", editable=False),
            FieldSpec("last_announced_at", "datetime", label="Last fired", editable=False),
            FieldSpec("created_at", "datetime", label="Created", editable=False),
        ]

    def display_summary(self, record: dict) -> RecordSummary:
        text = record.get("text") or "Reminder"
        due_at = record.get("due_at")
        subtitle = ReminderService.format_due_at_human(due_at) if due_at else None
        recurrence = record.get("recurrence")
        if recurrence and subtitle:
            subtitle = f"{subtitle} • {recurrence}"
        elif recurrence:
            subtitle = recurrence
        return RecordSummary(title=str(text), subtitle=subtitle, icon="bell")

    # ── Examples ──────────────────────────────────────────────────────

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                "Remind me to call mom tomorrow at 3 PM",
                {"action": "set", "text": "call mom", "resolved_datetimes": ["tomorrow"], "time": "15:00"},
                is_primary=True,
            ),
            CommandExample(
                "Remind me to take out the trash in 30 minutes",
                {"action": "set", "text": "take out the trash", "relative_minutes": 30},
            ),
            CommandExample(
                "Remind me every day at 8 AM to take my medicine",
                {"action": "set", "text": "take my medicine", "time": "08:00", "recurrence": "daily"},
            ),
            CommandExample(
                "Remind me every Monday at 9 AM to submit my timesheet",
                {"action": "set", "text": "submit my timesheet", "resolved_datetimes": ["next_monday"], "time": "09:00", "recurrence": "weekly"},
            ),
            CommandExample(
                "Remind me every other Sunday at noon to call grandma",
                {"action": "set", "text": "call grandma", "resolved_datetimes": ["next_sunday"], "time": "12:00", "recurrence": "biweekly"},
            ),
            CommandExample(
                "What reminders do I have?",
                {"action": "list"},
            ),
            CommandExample(
                "Cancel my reminder to call mom",
                {"action": "delete", "text": "call mom"},
            ),
            CommandExample(
                "Snooze for 15 minutes",
                {"action": "snooze", "minutes": 15},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items: list[tuple[str, dict[str, Any], bool]] = [
            # Set — date_keys + time
            ("Remind me to call mom tomorrow at 3 PM", {"action": "set", "text": "call mom", "resolved_datetimes": ["tomorrow"], "time": "15:00"}, True),
            ("Reminder to pick up dry cleaning on Monday", {"action": "set", "text": "pick up dry cleaning", "resolved_datetimes": ["next_monday"]}, False),
            ("Set a reminder for tonight to take out the trash", {"action": "set", "text": "take out the trash", "resolved_datetimes": ["tonight"]}, False),
            ("Remind me next Friday to pay rent", {"action": "set", "text": "pay rent", "resolved_datetimes": ["next_friday"]}, False),
            ("Remind me tomorrow morning to water the plants", {"action": "set", "text": "water the plants", "resolved_datetimes": ["tomorrow_morning"]}, False),
            ("Set a reminder for 5 PM today to leave work", {"action": "set", "text": "leave work", "resolved_datetimes": ["today"], "time": "17:00"}, False),
            ("Remind me next Tuesday at noon to pick up the package", {"action": "set", "text": "pick up the package", "resolved_datetimes": ["next_tuesday"], "time": "12:00"}, False),
            ("Remind me tomorrow evening to call grandma", {"action": "set", "text": "call grandma", "resolved_datetimes": ["tomorrow_evening"]}, False),
            ("Set a reminder for Monday morning to buy groceries", {"action": "set", "text": "buy groceries", "resolved_datetimes": ["next_monday", "morning"]}, False),
            ("Remind me at 6 PM to start dinner", {"action": "set", "text": "start dinner", "time": "18:00"}, False),
            # Set — relative minutes
            ("Remind me in 30 minutes to check the laundry", {"action": "set", "text": "check the laundry", "relative_minutes": 30}, False),
            ("Remind me in 2 hours to move the car", {"action": "set", "text": "move the car", "relative_minutes": 120}, False),
            ("Remind me in an hour to call the dentist", {"action": "set", "text": "call the dentist", "relative_minutes": 60}, False),
            ("Remind me in 15 minutes to flip the chicken", {"action": "set", "text": "flip the chicken", "relative_minutes": 15}, False),
            ("Remind me to check the oven in 45 minutes", {"action": "set", "text": "check the oven", "relative_minutes": 45}, False),
            # Set — recurrence
            ("Remind me every day at 8 to take my medicine", {"action": "set", "text": "take my medicine", "time": "08:00", "recurrence": "daily"}, False),
            ("Set a daily reminder at 7 AM to exercise", {"action": "set", "text": "exercise", "time": "07:00", "recurrence": "daily"}, False),
            ("Remind me on weekdays at 8:30 to check email", {"action": "set", "text": "check email", "time": "08:30", "recurrence": "weekdays"}, False),
            ("Remind me every morning to make the bed", {"action": "set", "text": "make the bed", "resolved_datetimes": ["morning"], "recurrence": "daily"}, False),
            ("Remind me every Monday at 9 AM to submit my timesheet", {"action": "set", "text": "submit my timesheet", "resolved_datetimes": ["next_monday"], "time": "09:00", "recurrence": "weekly"}, False),
            ("Remind me every Tuesday at 7 PM to take out the trash", {"action": "set", "text": "take out the trash", "resolved_datetimes": ["next_tuesday"], "time": "19:00", "recurrence": "weekly"}, False),
            ("Remind me every other Sunday at noon to call grandma", {"action": "set", "text": "call grandma", "resolved_datetimes": ["next_sunday"], "time": "12:00", "recurrence": "biweekly"}, False),
            ("Set a biweekly reminder on Friday at 5 PM to do payroll", {"action": "set", "text": "do payroll", "resolved_datetimes": ["next_friday"], "time": "17:00", "recurrence": "biweekly"}, False),
            ("Remind me on the 1st of every month to pay rent", {"action": "set", "text": "pay rent", "time": "09:00", "recurrence": "monthly"}, False),
            # List
            ("What reminders do I have?", {"action": "list"}, False),
            ("Any reminders for today?", {"action": "list", "filter": "today"}, False),
            ("Show my recurring reminders", {"action": "list", "filter": "recurring"}, False),
            ("Do I have any upcoming reminders?", {"action": "list"}, False),
            ("List my reminders", {"action": "list"}, False),
            # Delete
            ("Cancel my reminder to call mom", {"action": "delete", "text": "call mom"}, False),
            ("Delete the medicine reminder", {"action": "delete", "text": "medicine"}, False),
            ("Delete all my reminders", {"action": "delete", "scope": "all"}, False),
            ("Cancel the reminder about groceries", {"action": "delete", "text": "groceries"}, False),
            # Snooze
            ("Snooze that reminder", {"action": "snooze"}, False),
            ("Snooze for 30 minutes", {"action": "snooze", "minutes": 30}, False),
            ("Snooze the reminder about mom for 15 minutes", {"action": "snooze", "text": "mom", "minutes": 15}, False),
        ]
        return [
            CommandExample(voice, params, is_primary)
            for voice, params, is_primary in items
        ]

    # ── Pre-route ─────────────────────────────────────────────────────
    # Only the deterministic actions (list, delete-all, snooze) get
    # fast-paths. Set/delete-by-label fall through to the LLM because
    # they involve fuzzy text matching the LLM handles better.

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="reminder.list_today",
                description="Bypass LLM for 'any reminders for today' / 'reminders for today'",
                example="any reminders for today",
                regex=_LIST_TODAY_RE.pattern,
                handler="_fp_list_today",
            ),
            FastPathPattern(
                id="reminder.list_recurring",
                description="Bypass LLM for 'show my recurring reminders' / 'list recurring reminders'",
                example="show my recurring reminders",
                regex=_LIST_RECURRING_RE.pattern,
                handler="_fp_list_recurring",
            ),
            FastPathPattern(
                id="reminder.list",
                description="Bypass LLM for 'list my reminders' / 'what reminders do I have'",
                example="what reminders do I have",
                regex=_LIST_RE.pattern,
                handler="_fp_list_all",
            ),
            FastPathPattern(
                id="reminder.delete_all",
                description="Bypass LLM for 'delete all my reminders' / 'clear all reminders'",
                example="delete all my reminders",
                regex=_DELETE_ALL_RE.pattern,
                handler="_fp_delete_all",
            ),
            FastPathPattern(
                id="reminder.snooze_for",
                description="Bypass LLM for 'snooze for N minutes/hours'",
                example="snooze for 15 minutes",
                regex=_SNOOZE_FOR_RE.pattern,
                handler="_fp_snooze_for",
            ),
            FastPathPattern(
                id="reminder.snooze",
                description="Bypass LLM for bare 'snooze' / 'snooze that' (uses default 10 min)",
                example="snooze",
                regex=_SNOOZE_BARE_RE.pattern,
                handler="_fp_snooze_bare",
            ),
        ]

    def _fp_list_all(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "list"})

    def _fp_list_today(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "list", "filter": "today"})

    def _fp_list_recurring(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "list", "filter": "recurring"})

    def _fp_delete_all(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "delete", "scope": "all"})

    def _fp_snooze_bare(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"action": "snooze"})

    def _fp_snooze_for(self, match, voice_command: str) -> PreRouteResult | None:
        n = int(match.group("n"))
        unit = match.group("unit").lower()
        minutes = n * 60 if unit.startswith("hour") or unit.startswith("hr") else n
        return PreRouteResult(arguments={"action": "snooze", "minutes": minutes})

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        if not args.get("action"):
            # Default to "set" if text is present, otherwise "list"
            args["action"] = "set" if args.get("text") else "list"
        return args

    # ── Main execution ────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str = kwargs.get("action", "set")
        service = get_reminder_service()

        # Reminders are scoped to the recognized speaker. `user_id` may be None
        # for pre-routed calls without a recognized voice — list/delete/snooze
        # tolerate that (search falls back to legacy unscoped reminders), but
        # `set` requires identification so reminders can't bleed across users.
        speaker_user_id = request_info.user_id if request_info is not None else None

        if action == "set":
            return self._run_set(service, speaker_user_id, **kwargs)
        elif action == "list":
            return self._run_list(service, speaker_user_id, **kwargs)
        elif action == "delete":
            return self._run_delete(service, speaker_user_id, **kwargs)
        elif action == "snooze":
            return self._run_snooze(service, speaker_user_id, **kwargs)
        else:
            return CommandResponse.error_response(
                error_details=f"Unknown reminder action: {action}",
            )

    # ── Set ───────────────────────────────────────────────────────────

    def _run_set(
        self,
        service: ReminderService,
        user_id: int | None,
        **kwargs: Any,
    ) -> CommandResponse:
        text: str | None = kwargs.get("text")
        if not text:
            return CommandResponse.error_response(
                error_details="What should I remind you about?",
                context_data={"error": "missing_text", "message": "What should I remind you about?"},
                wait_for_input=True,
            )

        if user_id is None:
            return CommandResponse.error_response(
                error_details=(
                    "I couldn't tell who's asking, so I can't save a personal "
                    "reminder. Try again after Jarvis has recognized you."
                ),
                context_data={
                    "error": "unknown_speaker",
                    "message": (
                        "I'm not sure who's speaking — try training your voice "
                        "so I can save reminders for you."
                    ),
                },
            )

        date_keys: list[str] | None = kwargs.get("resolved_datetimes")
        time_str: str | None = kwargs.get("time")
        relative_minutes: int | None = kwargs.get("relative_minutes")
        recurrence: str | None = kwargs.get("recurrence")

        # If nothing time-related came in, ask the user instead of silently
        # defaulting to 9 AM. Pass wait_for_input=True so the voice listener
        # stays open for the answer.
        if not has_explicit_time(date_keys, time_str, relative_minutes):
            question = (
                f"What time should I remind you to {text}?"
                if recurrence is None
                else f"What time should I remind you to {text} {recurrence}?"
            )
            return CommandResponse.error_response(
                error_details=question,
                context_data={
                    "error": "missing_time",
                    "pending_text": text,
                    "pending_recurrence": recurrence,
                    "pending_date_keys": date_keys,
                    "message": question,
                },
                wait_for_input=True,
            )

        due_at = ReminderService.resolve_due_at(date_keys, time_str, relative_minutes)
        if due_at is None:
            return CommandResponse.error_response(
                error_details="When should I remind you? Please specify a date, time, or duration.",
                context_data={"error": "missing_time"},
            )

        if recurrence and recurrence not in _RECURRENCE_VALUES:
            return CommandResponse.error_response(
                error_details=f"Invalid recurrence: {recurrence}. Use: {', '.join(_RECURRENCE_VALUES)}",
            )

        reminder = service.create_reminder(text, due_at, recurrence, user_id=user_id)
        due_human = ReminderService.format_due_at_human(reminder.due_at)

        recurrence_str = f" ({recurrence})" if recurrence else ""
        message = f"Reminder set: {text} — {due_human}{recurrence_str}"

        return CommandResponse.success_response(
            context_data={
                "reminder_id": reminder.reminder_id,
                "text": text,
                "due_at": reminder.due_at,
                "due_at_human": due_human,
                "recurrence": recurrence,
                "message": message,
            },
            wait_for_input=False,
        )

    # ── List ──────────────────────────────────────────────────────────

    def _run_list(
        self,
        service: ReminderService,
        user_id: int | None,
        **kwargs: Any,
    ) -> CommandResponse:
        filter_type: str = kwargs.get("filter", "all")
        reminders = service.get_all_reminders(user_id=user_id)

        if filter_type == "today":
            from datetime import datetime, timezone
            today_local = datetime.now().astimezone().date()
            reminders = [
                r for r in reminders
                if datetime.fromisoformat(r.due_at).astimezone().date() == today_local
            ]
        elif filter_type == "recurring":
            reminders = [r for r in reminders if r.is_recurring]

        if not reminders:
            return CommandResponse.success_response(
                context_data={
                    "reminders": [],
                    "count": 0,
                    "message": "You don't have any reminders.",
                },
                wait_for_input=False,
            )

        formatted = [
            {
                "reminder_id": r.reminder_id,
                "text": r.text,
                "due_at": r.due_at,
                "due_at_human": ReminderService.format_due_at_human(r.due_at),
                "recurrence": r.recurrence,
            }
            for r in reminders
        ]

        return CommandResponse.success_response(
            context_data={
                "reminders": formatted,
                "count": len(formatted),
                "message": f"You have {len(formatted)} reminder(s).",
            },
            wait_for_input=False,
        )

    # ── Delete ────────────────────────────────────────────────────────

    def _run_delete(
        self,
        service: ReminderService,
        user_id: int | None,
        **kwargs: Any,
    ) -> CommandResponse:
        scope: str = kwargs.get("scope", "one")
        text: str | None = kwargs.get("text")

        if scope == "all":
            count = service.delete_all_reminders(user_id=user_id)
            return CommandResponse.success_response(
                context_data={
                    "deleted_count": count,
                    "message": f"Deleted all {count} reminder(s).",
                },
                wait_for_input=False,
            )

        if not text:
            return CommandResponse.error_response(
                error_details="Which reminder should I cancel? Describe it or say 'delete all'.",
                context_data={"error": "missing_text"},
            )

        reminder = service.find_by_text(text, user_id=user_id)
        if not reminder:
            return CommandResponse.error_response(
                error_details=f"No reminder found matching '{text}'.",
                context_data={"error": "not_found"},
            )

        service.delete_reminder(reminder.reminder_id)
        return CommandResponse.success_response(
            context_data={
                "deleted_count": 1,
                "deleted_text": reminder.text,
                "message": f"Cancelled your reminder: {reminder.text}",
            },
            wait_for_input=False,
        )

    # ── Snooze ────────────────────────────────────────────────────────

    def _run_snooze(
        self,
        service: ReminderService,
        user_id: int | None,
        **kwargs: Any,
    ) -> CommandResponse:
        minutes: int = kwargs.get("minutes") or 10
        text: str | None = kwargs.get("text")

        if text:
            reminder = service.find_by_text(text, user_id=user_id)
        else:
            reminder = service.find_most_recently_announced(user_id=user_id)

        if not reminder:
            return CommandResponse.error_response(
                error_details="No recently announced reminder to snooze.",
                context_data={"error": "no_recent_reminder"},
            )

        snoozed = service.snooze_reminder(reminder.reminder_id, minutes)
        if not snoozed:
            return CommandResponse.error_response(
                error_details="Failed to snooze the reminder.",
            )

        snooze_human = ReminderService.format_due_at_human(snoozed.snooze_until) if snoozed.snooze_until else f"in {minutes} minutes"

        return CommandResponse.success_response(
            context_data={
                "reminder_id": snoozed.reminder_id,
                "text": snoozed.text,
                "snoozed_until": snoozed.snooze_until,
                "snoozed_until_human": snooze_human,
                "message": f"Snoozed '{snoozed.text}' for {minutes} minutes.",
            },
            wait_for_input=False,
        )
