#!/usr/bin/env python3
"""
Check timers command for Jarvis.
Reports the status and remaining time of active timers.
"""

from typing import List

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from services.timer_service import get_timer_service


class CheckTimersCommand(IJarvisCommand):
    """Command for checking active timer status"""

    @property
    def command_name(self) -> str:
        return "check_timers"

    @property
    def description(self) -> str:
        return (
            "Check the status and remaining time of active timers. "
            "Can check a specific timer by label or get status of all timers."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "check timer", "timer status", "how much time",
            "time left", "time remaining", "what timers",
            "how long until"
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "label",
                "string",
                required=False,
                description=(
                    "Optional label to check a specific timer. "
                    "If omitted, returns status of all active timers."
                )
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "Extract timer label when asking about specific timer: 'how long until the pasta timer' → label='pasta'",
            "Omit label when asking about all timers: 'what timers are running' → no label",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return []

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration"""
        return [
            CommandExample(
                voice_command="How much time is left?",
                expected_parameters={},
                is_primary=True
            ),
            CommandExample(
                voice_command="Check the pasta timer",
                expected_parameters={"label": "pasta"}
            ),
            CommandExample(
                voice_command="What timers are running?",
                expected_parameters={}
            ),
            CommandExample(
                voice_command="How long until the egg timer?",
                expected_parameters={"label": "egg"}
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training (11 examples)"""
        items = [
            # General status queries
            ("How much time is left?", {}),
            ("What timers are running?", {}),
            ("Check my timers", {}),
            ("Timer status", {}),
            ("How long do I have left?", {}),

            # Specific timer queries
            ("Check the pasta timer", {"label": "pasta"}),
            ("How long until the egg timer?", {"label": "egg"}),
            ("What's the status of the laundry timer?", {"label": "laundry"}),
            ("How much time on the nap timer?", {"label": "nap"}),
            ("Is the cooking timer still running?", {"label": "cooking"}),
            ("Time left on my tea timer?", {"label": "tea"}),
        ]

        examples = []
        for i, (utterance, params) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters=params,
                is_primary=(i == 0)
            ))
        return examples

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the check timers command"""
        timer_service = get_timer_service()
        label = kwargs.get("label")

        active_timers = timer_service.get_active_timers()

        # No active timers
        if not active_timers:
            return CommandResponse.success_response(
                context_data={
                    "has_timers": False,
                    "timers": [],
                    "count": 0,
                    "message": "No active timers",
                },
                wait_for_input=False
            )

        # Check specific timer by label
        if label:
            timer_id = timer_service.find_timer_by_label(label)
            if timer_id:
                timer_info = timer_service.get_timer(timer_id)
                if timer_info:
                    remaining = timer_info.get("remaining_seconds", 0)
                    remaining_text = self._format_remaining(remaining)
                    timer_label = timer_info.get("label") or label

                    return CommandResponse.success_response(
                        context_data={
                            "has_timers": True,
                            "timers": [{
                                "timer_id": timer_id,
                                "label": timer_label,
                                "remaining_seconds": remaining,
                                "remaining_text": remaining_text,
                            }],
                            "count": 1,
                            "message": f"The {timer_label} timer has {remaining_text} remaining",
                        },
                        wait_for_input=False
                    )

            # No timer found with that label
            timer_labels = [
                t.get("label") or f"timer {t['timer_id']}"
                for t in active_timers
            ]
            return CommandResponse.success_response(
                context_data={
                    "has_timers": True,
                    "timers": [],
                    "count": len(active_timers),
                    "message": f"No timer found with label '{label}'",
                    "available_timers": timer_labels,
                    "suggestion": f"Available timers: {', '.join(timer_labels)}",
                },
                wait_for_input=False
            )

        # Return all timers
        timer_details = []
        for t in active_timers:
            remaining = t.get("remaining_seconds", 0)
            remaining_text = self._format_remaining(remaining)
            timer_label = t.get("label")

            timer_details.append({
                "timer_id": t["timer_id"],
                "label": timer_label,
                "remaining_seconds": remaining,
                "remaining_text": remaining_text,
            })

        # Build summary message
        if len(timer_details) == 1:
            t = timer_details[0]
            if t["label"]:
                message = f"The {t['label']} timer has {t['remaining_text']} remaining"
            else:
                message = f"Your timer has {t['remaining_text']} remaining"
        else:
            parts = []
            for t in timer_details:
                if t["label"]:
                    parts.append(f"the {t['label']} timer has {t['remaining_text']}")
                else:
                    parts.append(f"a timer has {t['remaining_text']}")
            message = "You have " + ", and ".join(parts) + " remaining"

        return CommandResponse.success_response(
            context_data={
                "has_timers": True,
                "timers": timer_details,
                "count": len(timer_details),
                "message": message,
            },
            wait_for_input=False
        )

    def _format_remaining(self, seconds: int) -> str:
        """Format remaining seconds into human-readable text"""
        if seconds < 60:
            return f"{seconds} second{'s' if seconds != 1 else ''}"

        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        remaining_seconds = seconds % 60

        parts = []
        if hours > 0:
            parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
        if minutes > 0:
            parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
        if remaining_seconds > 0 and hours == 0:
            # Only show seconds if under an hour
            parts.append(f"{remaining_seconds} second{'s' if remaining_seconds != 1 else ''}")

        if len(parts) == 1:
            return parts[0]
        elif len(parts) == 2:
            return f"{parts[0]} and {parts[1]}"
        else:
            return f"{parts[0]}, {parts[1]}, and {parts[2]}"
