#!/usr/bin/env python3
"""
Timer command for Jarvis.
Sets timers that run in the background and announce via TTS when complete.
"""

from typing import List

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from services.timer_service import get_timer_service


class TimerCommand(IJarvisCommand):
    """Command for setting timers with optional labels"""

    @property
    def command_name(self) -> str:
        return "set_timer"

    @property
    def description(self) -> str:
        return (
            "Set a timer for a specified duration. The timer runs in the background "
            "and announces via voice when complete. Convert spoken time to total seconds "
            "(e.g., '5 minutes' = 300, '1 hour 30 minutes' = 5400)."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "timer", "set timer", "alarm", "remind", "reminder",
            "countdown", "wake me", "notify me"
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "duration_seconds",
                "int",
                required=True,
                description=(
                    "Total duration in seconds. Convert spoken time: "
                    "'30 seconds' → 30, '5 minutes' → 300, "
                    "'1 hour' → 3600, '1 hour 30 minutes' → 5400."
                )
            ),
            JarvisParameter(
                "label",
                "string",
                required=False,
                description=(
                    "Optional label for the timer (e.g., 'pasta', 'laundry', 'nap'). "
                    "Used in the completion announcement."
                )
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "Always convert spoken time to total seconds before calling",
            "Extract labels from context: 'timer for pasta' → label='pasta'",
            "Compound times must be summed: '2 minutes 30 seconds' → 150 seconds",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Time conversion: 1 minute = 60 seconds, 1 hour = 3600 seconds",
            "The duration_seconds parameter must be a positive integer",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration"""
        return [
            CommandExample(
                voice_command="Set a timer for 5 minutes",
                expected_parameters={"duration_seconds": 300},
                is_primary=True
            ),
            CommandExample(
                voice_command="Timer for 30 seconds",
                expected_parameters={"duration_seconds": 30}
            ),
            CommandExample(
                voice_command="Set a 10 minute timer for pasta",
                expected_parameters={"duration_seconds": 600, "label": "pasta"}
            ),
            CommandExample(
                voice_command="Remind me in 1 hour",
                expected_parameters={"duration_seconds": 3600}
            ),
            CommandExample(
                voice_command="Set a timer for 2 minutes and 30 seconds",
                expected_parameters={"duration_seconds": 150}
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training (19 examples)"""
        items = [
            # Seconds only
            ("Set a timer for 30 seconds", {"duration_seconds": 30}),
            ("Timer for 45 seconds", {"duration_seconds": 45}),
            ("Set a 15 second timer", {"duration_seconds": 15}),

            # Minutes only
            ("Set a timer for 5 minutes", {"duration_seconds": 300}),
            ("Timer for ten minutes", {"duration_seconds": 600}),
            ("Set a 15 minute timer", {"duration_seconds": 900}),
            ("3 minute timer please", {"duration_seconds": 180}),

            # Hours only
            ("Set a timer for 1 hour", {"duration_seconds": 3600}),
            ("Timer for 2 hours", {"duration_seconds": 7200}),

            # Compound times
            ("Set a timer for 1 hour and 30 minutes", {"duration_seconds": 5400}),
            ("Timer for 2 minutes 30 seconds", {"duration_seconds": 150}),
            ("Set a 1 hour 15 minute timer", {"duration_seconds": 4500}),

            # With labels
            ("Set a 10 minute timer for pasta", {"duration_seconds": 600, "label": "pasta"}),
            ("Timer for 20 minutes for the laundry", {"duration_seconds": 1200, "label": "laundry"}),
            ("Set a nap timer for 30 minutes", {"duration_seconds": 1800, "label": "nap"}),
            ("Egg timer for 7 minutes", {"duration_seconds": 420, "label": "egg"}),

            # Casual phrasing
            ("Remind me in 15 minutes", {"duration_seconds": 900}),
            ("Wake me up in 30 minutes", {"duration_seconds": 1800}),
            ("Let me know in an hour", {"duration_seconds": 3600}),
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
        """Execute the timer command"""
        try:
            duration_seconds = kwargs.get("duration_seconds")
            label = kwargs.get("label")

            # Validate duration
            if duration_seconds is None:
                return CommandResponse.error_response(
                    error_details="Duration is required",
                    context_data={"error": "missing_duration"}
                )

            duration_seconds = int(duration_seconds)
            if duration_seconds <= 0:
                return CommandResponse.error_response(
                    error_details="Duration must be positive",
                    context_data={
                        "error": "invalid_duration",
                        "duration_seconds": duration_seconds
                    }
                )

            # Set the timer
            timer_service = get_timer_service()
            timer_id = timer_service.set_timer(duration_seconds, label)

            # Format duration for response
            duration_text = self._format_duration(duration_seconds)

            return CommandResponse.success_response(
                context_data={
                    "timer_id": timer_id,
                    "duration_seconds": duration_seconds,
                    "duration_text": duration_text,
                    "label": label,
                    "message": self._build_confirmation_message(duration_text, label),
                },
                wait_for_input=False  # Timer set, no follow-up needed
            )

        except (ValueError, TypeError) as e:
            return CommandResponse.error_response(
                error_details=f"Invalid parameters: {str(e)}",
                context_data={
                    "error": str(e),
                    "parameters_received": kwargs
                }
            )
        except Exception as e:
            return CommandResponse.error_response(
                error_details=f"Timer error: {str(e)}",
                context_data={
                    "error": str(e),
                    "parameters_received": kwargs
                }
            )

    def _format_duration(self, seconds: int) -> str:
        """Format seconds into human-readable duration"""
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
        if remaining_seconds > 0:
            parts.append(f"{remaining_seconds} second{'s' if remaining_seconds != 1 else ''}")

        if len(parts) == 1:
            return parts[0]
        elif len(parts) == 2:
            return f"{parts[0]} and {parts[1]}"
        else:
            return f"{parts[0]}, {parts[1]}, and {parts[2]}"

    def _build_confirmation_message(self, duration_text: str, label: str | None) -> str:
        """Build the confirmation message for the response"""
        if label:
            return f"Timer set for {duration_text} for {label}"
        return f"Timer set for {duration_text}"
