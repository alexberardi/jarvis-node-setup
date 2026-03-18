"""RoutineCommand — execute multi-step voice routines (good morning, good night, etc.).

Pre-routes trigger phrases deterministically (no LLM), runs sub-commands
locally, then sends collected context to CC's chat_text() for a natural
composed spoken response.
"""

import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from core.command_response import CommandResponse
from core.ijarvis_command import (
    CommandExample,
    IJarvisCommand,
    PreRouteResult,
)
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from db import SessionLocal
from repositories.command_data_repository import CommandDataRepository
from utils.command_discovery_service import get_command_discovery_service
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

_COMMAND_NAME = "routine"

_LENGTH_INSTRUCTIONS: Dict[str, str] = {
    "short": "Respond in 2-4 spoken sentences, conversational tone.",
    "medium": "Respond in 6-10 spoken sentences, flowing narrative tone. Cover each topic with a sentence or two.",
    "long": "Respond in a detailed paragraph style, about 60 seconds of speech. Cover each topic thoroughly.",
}

_TYPE_DEFAULTS: Dict[str, str] = {
    "routine": "short",
    "briefing": "medium",
}


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _load_routines() -> Dict[str, Dict[str, Any]]:
    """Load routines from the local database.

    Each routine is stored as a row in command_data with
    command_name='routine' and data_key=routine_name.

    On first run (empty DB), seeds with built-in defaults.
    """
    try:
        db = SessionLocal()
        try:
            repo = CommandDataRepository(db)
            rows = repo.get_all(_COMMAND_NAME)

            routines: Dict[str, Dict[str, Any]] = {}
            for row in rows:
                key = row.pop("_data_key", None)
                row.pop("_expires_at", None)
                if key:
                    routines[key] = row

            # Merge missing defaults into existing DB rows
            defaults = RoutineCommand._default_routines()
            for name, definition in defaults.items():
                if name not in routines:
                    repo.save(_COMMAND_NAME, name, definition)
                    routines[name] = definition

            return routines
        finally:
            db.close()
    except Exception as e:
        logger.warning("Failed to load routines from DB, using defaults", error=str(e))
        return RoutineCommand._default_routines()


def save_routine(name: str, definition: Dict[str, Any]) -> None:
    """Save or update a routine in the local database."""
    db = SessionLocal()
    try:
        repo = CommandDataRepository(db)
        repo.save(_COMMAND_NAME, name, definition)
    finally:
        db.close()


def delete_routine(name: str) -> bool:
    """Delete a routine from the local database."""
    db = SessionLocal()
    try:
        repo = CommandDataRepository(db)
        return repo.delete(_COMMAND_NAME, name)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# RoutineCommand
# ---------------------------------------------------------------------------

class RoutineCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "routine"

    @property
    def description(self) -> str:
        return (
            "Execute a multi-step voice routine (e.g. good morning, good night). "
            "Runs sub-commands and composes a natural spoken response."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "routine", "good morning", "good night", "bedtime", "start my day",
            "morning routine", "briefing", "daily briefing", "morning briefing",
            "nightly briefing", "evening briefing", "nightly update",
            "catch me up", "daily update",
        ]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "routine_name",
                "string",
                required=True,
                description="Name of the routine to execute (e.g. good_morning, good_night).",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Good morning",
                expected_parameters={"routine_name": "good_morning"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Good night",
                expected_parameters={"routine_name": "good_night"},
            ),
            CommandExample(
                voice_command="Give me my morning briefing",
                expected_parameters={"routine_name": "morning_briefing"},
            ),
            CommandExample(
                voice_command="Give me my nightly briefing",
                expected_parameters={"routine_name": "nightly_briefing"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return self.generate_prompt_examples()

    # ------------------------------------------------------------------
    # Pre-routing — deterministic trigger phrase matching
    # ------------------------------------------------------------------

    def pre_route(self, voice_command: str) -> PreRouteResult | None:
        text = voice_command.strip().lower()
        if not text:
            return None

        routines = _load_routines()

        for routine_name, routine_def in routines.items():
            phrases = routine_def.get("trigger_phrases", [])
            if self._matches(text, phrases):
                logger.info("Routine pre-routed", routine=routine_name, voice_command=voice_command)
                return PreRouteResult(arguments={"routine_name": routine_name})

        return None

    @staticmethod
    def _matches(text: str, phrases: List[str]) -> bool:
        """Multi-strategy matching against trigger phrases.

        Strategies (in order):
        1. Exact match (case-insensitive, stripped)
        2. Substring: phrase appears in text ("good morning jarvis" contains "good morning")
        3. Reversed substring: text appears in phrase ("morning" in "morning routine")
        4. Keyword overlap: ≥80% of phrase tokens in text ("time for bed" ↔ "bedtime")
        """
        for phrase in phrases:
            phrase_lower = phrase.strip().lower()
            if not phrase_lower:
                continue

            # 1. Exact match
            if text == phrase_lower:
                return True

            # 2. Phrase is substring of text
            if phrase_lower in text:
                return True

            # 3. Text is substring of phrase (short utterance matches longer trigger)
            if len(text) >= 3 and text in phrase_lower:
                return True

            # 4. Keyword overlap — tokenize and check ≥80% overlap
            phrase_tokens = set(phrase_lower.split())
            text_tokens = set(text.split())
            if phrase_tokens and text_tokens:
                overlap = phrase_tokens & text_tokens
                # Check both directions: phrase tokens in text, and text tokens in phrase
                if len(overlap) / len(phrase_tokens) >= 0.8:
                    return True
                if len(overlap) / len(text_tokens) >= 0.8:
                    return True

        return False

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        routine_name: str = kwargs["routine_name"]
        routines = _load_routines()

        routine_def = routines.get(routine_name)
        if not routine_def:
            return CommandResponse.error_response(
                error_details=f"Unknown routine: {routine_name}",
            )

        steps = routine_def.get("steps", [])
        if not steps:
            return CommandResponse.error_response(
                error_details=f"Routine '{routine_name}' has no steps configured.",
            )

        instruction = routine_def.get("response_instruction", "Summarize the results conversationally.")
        routine_type = routine_def.get("type", "routine")
        response_length = routine_def.get(
            "response_length",
            _TYPE_DEFAULTS.get(routine_type, "short"),
        )
        discovery = get_command_discovery_service()

        results: Dict[str, Any] = {}
        errors: Dict[str, str] = {}

        for step in steps:
            cmd_name = step.get("command", "")
            args = dict(step.get("args", {}))  # copy so we don't mutate the definition
            label = step.get("label", cmd_name)

            # Resolve relative date keywords to actual YYYY-MM-DD dates
            if "resolved_datetimes" in args:
                args["resolved_datetimes"] = self._resolve_dates(args["resolved_datetimes"])

            command = discovery.get_command(cmd_name)
            if command is None:
                logger.warning("Routine step skipped — command not found", command=cmd_name, routine=routine_name)
                errors[label] = f"Command '{cmd_name}' not available"
                continue

            try:
                response = command.execute(request_info, **args)
                if response.success:
                    results[label] = response.context_data or {}
                else:
                    errors[label] = response.error_details or "Unknown error"
                    logger.warning("Routine step failed", label=label, error=response.error_details)
            except Exception as e:
                errors[label] = str(e)
                logger.warning("Routine step exception", label=label, error=str(e))

        # All steps failed
        if not results and errors:
            return CommandResponse.error_response(
                error_details="All routine steps failed.",
                context_data={"errors": errors},
            )

        # Compose response via LLM (with fallback)
        composed = self._compose_response(results, errors, instruction, response_length)

        return CommandResponse.success_response(
            context_data={"message": composed},
            wait_for_input=False,
        )

    def _compose_response(
        self,
        results: Dict[str, Any],
        errors: Dict[str, str],
        instruction: str,
        response_length: str = "short",
    ) -> str:
        """Compose a natural spoken response via CC's chat_text(), with fallback."""
        length_instruction = _LENGTH_INSTRUCTIONS.get(
            response_length,
            _LENGTH_INSTRUCTIONS["short"],
        )
        prompt = (
            f"/no_think\n{instruction}\n\n"
            f"Here are the results from each step:\n"
            f"{json.dumps(results, indent=2, default=str)}\n\n"
            f"{length_instruction} "
            "Do not mention steps that failed — only include information from successful steps."
        )

        if errors:
            prompt += f"\n\nThese steps had errors (do not mention them): {json.dumps(errors)}"

        try:
            cc_url = get_command_center_url()
            client = JarvisCommandCenterClient(cc_url)
            composed = client.chat_text(prompt)
            if composed:
                # Strip <think>...</think> blocks from reasoning models
                composed = re.sub(r"<think>.*?</think>\s*", "", composed, flags=re.DOTALL)
                return composed.strip()
        except Exception as e:
            logger.warning("LLM composition failed, using fallback", error=str(e))

        # Fallback: concatenate messages from results
        return self._fallback_compose(results)

    @staticmethod
    def _fallback_compose(results: Dict[str, Any]) -> str:
        """Simple concatenation fallback when LLM is unavailable."""
        parts: List[str] = []
        for label, data in results.items():
            if isinstance(data, dict):
                msg = data.get("message")
                if msg and isinstance(msg, str):
                    parts.append(msg)
        return " ".join(parts) if parts else "Routine completed."

    # ------------------------------------------------------------------
    # Date resolution
    # ------------------------------------------------------------------

    _RELATIVE_DATE_OFFSETS: Dict[str, int] = {
        "today": 0,
        "tomorrow": 1,
        "yesterday": -1,
        "day_after_tomorrow": 2,
    }

    @staticmethod
    def _resolve_dates(date_values: List[str]) -> List[str]:
        """Resolve relative date keywords (today, tomorrow) to YYYY-MM-DD strings."""
        resolved: List[str] = []
        now = datetime.now()
        for val in date_values:
            offset = RoutineCommand._RELATIVE_DATE_OFFSETS.get(val.lower().strip())
            if offset is not None:
                resolved.append((now + timedelta(days=offset)).strftime("%Y-%m-%d"))
            else:
                # Already an absolute date or unknown — pass through
                resolved.append(val)
        return resolved

    # ------------------------------------------------------------------
    # Default routines
    # ------------------------------------------------------------------

    @staticmethod
    def _default_routines() -> Dict[str, Dict[str, Any]]:
        """Built-in routine definitions used when config has no 'routines' key."""
        return {
            "good_morning": {
                "trigger_phrases": ["good morning", "morning routine", "start my day"],
                "steps": [
                    {"command": "control_device", "args": {"floor": "Downstairs", "action": "turn_on"}, "label": "lights"},
                    {"command": "get_weather", "args": {"resolved_datetimes": ["today"]}, "label": "weather"},
                    {"command": "get_calendar_events", "args": {"resolved_datetimes": ["today"]}, "label": "calendar"},
                ],
                "response_instruction": "Give a cheerful morning briefing with weather and calendar highlights.",
            },
            "good_night": {
                "trigger_phrases": ["good night", "bedtime", "going to bed", "time for bed"],
                "steps": [
                    {"command": "control_device", "args": {"floor": "Downstairs", "action": "turn_off"}, "label": "lights"},
                    {"command": "get_calendar_events", "args": {"resolved_datetimes": ["tomorrow"]}, "label": "tomorrow"},
                ],
                "response_instruction": "Brief goodnight with tomorrow's first appointment if any.",
            },
            "morning_briefing": {
                "type": "briefing",
                "response_length": "medium",
                "trigger_phrases": [
                    "morning briefing", "daily briefing",
                    "give me my briefing", "what's happening today",
                    "catch me up", "daily update",
                ],
                "steps": [
                    {"command": "get_weather", "args": {"resolved_datetimes": ["today"]}, "label": "weather"},
                    {"command": "get_calendar_events", "args": {"resolved_datetimes": ["today"]}, "label": "calendar"},
                    {"command": "get_news", "args": {"category": "general", "count": 3}, "label": "news"},
                ],
                "response_instruction": (
                    "Deliver a morning briefing in a natural, flowing narrative style. "
                    "Start with today's weather, then mention calendar events, "
                    "then summarize the top news headlines. Sound like a personal "
                    "news anchor, not a list of bullet points."
                ),
            },
            "nightly_briefing": {
                "type": "briefing",
                "response_length": "medium",
                "trigger_phrases": [
                    "nightly briefing", "evening briefing",
                    "nightly update", "evening update",
                    "what happened today", "end of day briefing",
                ],
                "steps": [
                    {"command": "get_weather", "args": {"resolved_datetimes": ["tomorrow"]}, "label": "tomorrow_weather"},
                    {"command": "get_calendar_events", "args": {"resolved_datetimes": ["tomorrow"]}, "label": "tomorrow_calendar"},
                    {"command": "get_news", "args": {"category": "general", "count": 3}, "label": "news"},
                ],
                "response_instruction": (
                    "Deliver an evening briefing in a calm, winding-down tone. "
                    "Start with tomorrow's weather outlook, then mention any "
                    "calendar events for tomorrow, then summarize today's top "
                    "news headlines. Keep it relaxed and conversational."
                ),
            },
        }
