"""Reminder command for Jarvis.

Unified command for setting, listing, deleting, and snoozing reminders.
Reminders persist across restarts and support recurrence (daily, weekly,
weekdays, monthly).
"""

from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from core.ijarvis_command import CommandAntipattern, CommandExample, IJarvisCommand, PreRouteResult
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from services.reminder_service import get_reminder_service, ReminderService

logger = JarvisLogger(service="jarvis-node")

_ALL_ACTIONS = ["set", "list", "delete", "snooze"]

_RECURRENCE_VALUES = ["daily", "weekly", "weekdays", "monthly"]


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
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "Default action is 'set' when user says 'remind me...'",
            "For 'in 30 minutes' or 'in 2 hours', use relative_minutes (30, 120)",
            "For 'at 3 PM', use time='15:00'",
            "For 'tomorrow at 3 PM', use resolved_datetimes=['tomorrow'] + time='15:00'",
            "For 'every day at 8 AM', use time='08:00' + recurrence='daily'",
            "The text parameter should capture WHAT to be reminded about, not WHEN",
            "'snooze' with no target snoozes the most recently fired reminder",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "text is ALWAYS required for action='set' — if unclear, ask 'What should I remind you about?'",
            "At least one time parameter (resolved_datetimes, time, or relative_minutes) is required for 'set'",
            "This is for reminders (absolute time), NOT timers (relative duration countdowns)",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern("Set a timer for 5 minutes", "set_timer"),
            CommandAntipattern("Timer for 30 seconds", "set_timer"),
            CommandAntipattern("Countdown from 10", "set_timer"),
            CommandAntipattern("Wake me up in 5 minutes", "set_timer"),
        ]

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
            ("Remind me every Monday at 9 AM to submit my timesheet", {"action": "set", "text": "submit my timesheet", "time": "09:00", "recurrence": "weekly"}, False),
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

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        if not args.get("action"):
            # Default to "set" if text is present, otherwise "list"
            args["action"] = "set" if args.get("text") else "list"
        return args

    # ── Main execution ────────────────────────────────────────────────

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        action: str = kwargs.get("action", "set")
        service = get_reminder_service()

        if action == "set":
            return self._run_set(service, **kwargs)
        elif action == "list":
            return self._run_list(service, **kwargs)
        elif action == "delete":
            return self._run_delete(service, **kwargs)
        elif action == "snooze":
            return self._run_snooze(service, **kwargs)
        else:
            return CommandResponse.error_response(
                error_details=f"Unknown reminder action: {action}",
            )

    # ── Set ───────────────────────────────────────────────────────────

    def _run_set(self, service: ReminderService, **kwargs: Any) -> CommandResponse:
        text: str | None = kwargs.get("text")
        if not text:
            return CommandResponse.error_response(
                error_details="What should I remind you about?",
                context_data={"error": "missing_text"},
            )

        date_keys: list[str] | None = kwargs.get("resolved_datetimes")
        time_str: str | None = kwargs.get("time")
        relative_minutes: int | None = kwargs.get("relative_minutes")
        recurrence: str | None = kwargs.get("recurrence")

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

        reminder = service.create_reminder(text, due_at, recurrence)
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

    def _run_list(self, service: ReminderService, **kwargs: Any) -> CommandResponse:
        filter_type: str = kwargs.get("filter", "all")
        reminders = service.get_all_reminders()

        if filter_type == "today":
            from datetime import datetime, timezone
            today = datetime.now(timezone.utc).date()
            reminders = [
                r for r in reminders
                if datetime.fromisoformat(r.due_at).date() == today
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

    def _run_delete(self, service: ReminderService, **kwargs: Any) -> CommandResponse:
        scope: str = kwargs.get("scope", "one")
        text: str | None = kwargs.get("text")

        if scope == "all":
            count = service.delete_all_reminders()
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

        reminder = service.find_by_text(text)
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

    def _run_snooze(self, service: ReminderService, **kwargs: Any) -> CommandResponse:
        minutes: int = kwargs.get("minutes") or 10
        text: str | None = kwargs.get("text")

        if text:
            reminder = service.find_by_text(text)
        else:
            reminder = service.find_most_recently_announced()

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
