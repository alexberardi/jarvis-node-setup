#!/usr/bin/env python3
"""
Cancel timer command for Jarvis.
Cancels active timers by label or cancels all timers.
"""

import re
from typing import List

from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandExample,
    FastPathPattern,
    IJarvisCommand,
    PreRouteResult,
)
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from services.timer_service import get_timer_service


# --- Pre-route patterns ---

# Cancel-all: "cancel all timers", "stop all my timers", "clear all timers"
_CANCEL_ALL_RE = re.compile(
    r"^\s*(?:cancel|stop|clear|remove|delete)\s+all(?:\s+(?:my|the))?\s+timers?\s*[?.!]*$",
    re.IGNORECASE,
)

# Cancel-by-label: "cancel the pasta timer", "stop the egg timer", "remove the nap timer"
_CANCEL_LABELED_RE = re.compile(
    r"^\s*(?:cancel|stop|remove|delete|never\s+mind)\s+"
    r"(?:the\s+|my\s+)?(?P<label>[a-zA-Z][a-zA-Z\s\-]+?)\s+timer\s*[?.!]*$",
    re.IGNORECASE,
)

# Cancel-no-label: "cancel my timer", "stop the timer", "cancel timer", "never mind the timer"
_CANCEL_NONE_RE = re.compile(
    r"^\s*(?:cancel|stop|never\s+mind)\s+(?:my\s+|the\s+)?timer\s*[?.!]*$",
    re.IGNORECASE,
)


class CancelTimerCommand(IJarvisCommand):
    """Command for cancelling active timers"""

    @property
    def command_name(self) -> str:
        return "cancel_timer"

    @property
    def description(self) -> str:
        return "Cancel timer by label, 'all' to cancel all, or omit label for single timer."

    @property
    def keywords(self) -> List[str]:
        return [
            "cancel timer", "stop timer", "cancel alarm",
            "stop alarm", "remove timer", "delete timer"
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "label",
                "string",
                required=False,
                description=(
                    "Label of timer to cancel. Use 'all' to cancel all timers. "
                    "If omitted and only one timer exists, cancels that one."
                )
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "Extract timer label from context: 'cancel the pasta timer' → label='pasta'",
            "Use 'all' to cancel multiple timers: 'cancel all timers' → label='all'",
            "Omit label for single-timer scenarios: 'cancel my timer' → no label",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Only extract the timer's subject as the label, not 'timer' or 'my'",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration"""
        return [
            CommandExample(
                voice_command="Cancel the pasta timer",
                expected_parameters={"label": "pasta"},
                is_primary=True
            ),
            CommandExample(
                voice_command="Cancel all timers",
                expected_parameters={"label": "all"}
            ),
            CommandExample(
                voice_command="Cancel my timer",
                expected_parameters={}
            ),
            CommandExample(
                voice_command="Stop the egg timer",
                expected_parameters={"label": "egg"}
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training (12 examples)"""
        items = [
            # Cancel by label
            ("Cancel the pasta timer", {"label": "pasta"}),
            ("Stop the egg timer", {"label": "egg"}),
            ("Cancel my laundry timer", {"label": "laundry"}),
            ("Remove the nap timer", {"label": "nap"}),
            ("Stop the cooking timer", {"label": "cooking"}),

            # Cancel all
            ("Cancel all timers", {"label": "all"}),
            ("Stop all my timers", {"label": "all"}),
            ("Clear all timers", {"label": "all"}),

            # Cancel without label (single timer scenario)
            ("Cancel my timer", {}),
            ("Stop the timer", {}),
            ("Cancel timer", {}),
            ("Never mind the timer", {}),
        ]

        examples = []
        for i, (utterance, params) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters=params,
                is_primary=(i == 0)
            ))
        return examples

    # ------------------------------------------------------------------
    # Fast-path patterns — bypass the LLM for the common cancel phrasings.
    # Order matters: cancel-all and cancel-no-label run before cancel-labeled
    # so "cancel timer" / "cancel all timers" aren't misread as labels.
    # ------------------------------------------------------------------
    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id="cancel_timer.all",
                description="Bypass LLM for 'cancel all timers' / 'stop all my timers'",
                example="cancel all timers",
                regex=_CANCEL_ALL_RE.pattern,
                handler="_fp_cancel_all",
            ),
            FastPathPattern(
                id="cancel_timer.none",
                description="Bypass LLM for 'cancel my timer' / 'stop the timer' (no specific label)",
                example="cancel my timer",
                regex=_CANCEL_NONE_RE.pattern,
                handler="_fp_cancel_none",
            ),
            FastPathPattern(
                id="cancel_timer.labeled",
                description="Bypass LLM for 'cancel the <label> timer' / 'stop the <label> timer'",
                example="cancel the pasta timer",
                regex=_CANCEL_LABELED_RE.pattern,
                handler="_fp_cancel_labeled",
            ),
        ]

    def _fp_cancel_all(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={"label": "all"})

    def _fp_cancel_none(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={})

    def _fp_cancel_labeled(self, match, voice_command: str) -> PreRouteResult | None:
        label = match.group("label").strip()
        if not label or label.lower() in ("all", "my", "the"):
            return None
        return PreRouteResult(arguments={"label": label})

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the cancel timer command"""
        timer_service = get_timer_service()
        label = kwargs.get("label")

        active_timers = timer_service.get_active_timers()

        # No active timers
        if not active_timers:
            return CommandResponse.success_response(
                context_data={
                    "cancelled": False,
                    "message": "No active timers to cancel",
                    "timer_count": 0,
                },
                wait_for_input=False
            )

        # Cancel all timers
        if label and label.lower() == "all":
            count = timer_service.clear_all()
            return CommandResponse.success_response(
                context_data={
                    "cancelled": True,
                    "cancelled_count": count,
                    "message": f"Cancelled all {count} timer{'s' if count != 1 else ''}",
                },
                wait_for_input=False
            )

        # Cancel by label
        if label:
            timer_id = timer_service.find_timer_by_label(label)
            if timer_id:
                timer_info = timer_service.get_timer(timer_id)
                timer_service.cancel_timer(timer_id)
                return CommandResponse.success_response(
                    context_data={
                        "cancelled": True,
                        "timer_id": timer_id,
                        "label": timer_info.get("label") if timer_info else label,
                        "message": f"Cancelled the {label} timer",
                    },
                    wait_for_input=False
                )
            else:
                # No timer found with that label
                timer_labels = [
                    t.get("label") or f"timer {t['timer_id']}"
                    for t in active_timers
                ]
                return CommandResponse.success_response(
                    context_data={
                        "cancelled": False,
                        "message": f"No timer found with label '{label}'",
                        "available_timers": timer_labels,
                        "suggestion": f"Available timers: {', '.join(timer_labels)}",
                    },
                    wait_for_input=False
                )

        # No label provided
        if len(active_timers) == 1:
            # Only one timer - cancel it
            timer = active_timers[0]
            timer_service.cancel_timer(timer["timer_id"])
            timer_label = timer.get("label")
            if timer_label:
                message = f"Cancelled the {timer_label} timer"
            else:
                message = "Cancelled your timer"
            return CommandResponse.success_response(
                context_data={
                    "cancelled": True,
                    "timer_id": timer["timer_id"],
                    "label": timer_label,
                    "message": message,
                },
                wait_for_input=False
            )
        else:
            # Multiple timers - ask which one
            timer_descriptions = []
            for t in active_timers:
                label_str = t.get("label") or f"timer {t['timer_id']}"
                remaining = t.get("remaining_seconds", 0)
                remaining_text = self._format_remaining(remaining)
                timer_descriptions.append(f"{label_str} ({remaining_text} remaining)")

            return CommandResponse.success_response(
                context_data={
                    "cancelled": False,
                    "message": "Which timer would you like to cancel?",
                    "active_timers": timer_descriptions,
                    "timer_count": len(active_timers),
                    "needs_clarification": True,
                },
                wait_for_input=True  # Waiting for user to specify which timer
            )

    def _format_remaining(self, seconds: int) -> str:
        """Format remaining seconds into human-readable text"""
        if seconds < 60:
            return f"{seconds} second{'s' if seconds != 1 else ''}"

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60

        parts = []
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")

        return " and ".join(parts) if len(parts) <= 2 else ", ".join(parts)
