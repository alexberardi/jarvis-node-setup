"""WhatsUpCommand — deliver or silently dismiss pending alerts via voice.

Two fast-path patterns:

* ``check_alerts.greeting`` — "what's up", "any alerts" etc. flush the
  queue and ANNOUNCE the alerts via CC composition.
* ``check_alerts.dismiss`` — "clear my alerts", "dismiss notifications"
  etc. flush the queue SILENTLY (just a brief "Cleared." reply) so the
  user can drop the alert-pending LED without sitting through every
  announcement. Matched by verb+noun regex, not an exact phrase list —
  "clear my alerts" once fell through to the LLM because only "clear
  alerts" was listed, and the LED stayed purple.

If no alerts and the greeting fast-path triggers, returns None so the
LLM handles it as casual conversation. The dismiss fast-path always
matches — clearing an empty queue is a valid intent.

``run()`` is also reachable through normal LLM routing (no pre-route
arguments). That path reads and flushes the LIVE queue — it used to
answer "No pending alerts." unconditionally, leaving queued alerts and
a purple LED behind. An optional ``dismiss`` parameter lets the LLM
route dismissal phrasings the regex misses.
"""

import json
import re
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandExample,
    FastPathPattern,
    IJarvisCommand,
    PreRouteResult,
    ValidationResult,
)
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from services.alert_queue_service import get_alert_queue_service
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

_TRIGGER_PHRASES = [
    "what's up",
    "whats up",
    "what's new",
    "whats new",
    "any alerts",
    "any updates",
    "anything new",
    "what did i miss",
    "any notifications",
    "check alerts",
    "check notifications",
]

# Dismiss intent: a clearing verb followed by an alert noun, with only a
# closed set of determiner/filler tokens allowed between them. A free-form
# gap fused verbs and nouns across clauses ("cancel the timer and read my
# notifications" must not flush the queue); the closed set still covers
# "clear my alerts", "dismiss all of the notifications", "cancel that
# alert" — so phrasing variants can't silently fall through to the LLM.
# The trailing lookahead rejects non-alert head nouns ("notification
# sound/settings"). Pre-route runs against EVERY utterance and the flush
# is destructive, so precision beats recall here — the LLM-routed
# ``dismiss`` parameter is the backstop for missed phrasings.
_DISMISS_RE = re.compile(
    r"\b(clear|dismiss|cancel|remove|delete)\b"
    r"(?:\s+(?:all|my|the|those|these|that|any|old|pending|of|please))*"
    r"\s+(alerts?|notifications?)\b"
    r"(?!\s+(?:sound|sounds|setting|settings|tone|tones|volume))"
)

# Negated/questioned clearing verbs must not flush ("don't clear my alerts").
_DISMISS_NEGATION_RE = re.compile(
    r"\b(?:don'?t|do not|didn'?t|never|won'?t|not|gonna)\s+"
    r"(?:clear|dismiss|cancel|remove|delete)\b"
)


class WhatsUpCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "check_alerts"

    @property
    def description(self) -> str:
        return "Check and deliver pending background alerts (news, calendar, etc.)."

    @property
    def keywords(self) -> List[str]:
        return ["alerts", "updates", "what's up", "what's new", "notifications"]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "alerts_json",
                "string",
                required=False,
                description="JSON-encoded alert data (set by pre-route, not user).",
            ),
            JarvisParameter(
                "dismiss",
                "boolean",
                required=False,
                description="Set true when the user wants alerts/notifications cleared or dismissed without hearing them.",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="What's up?",
                expected_parameters={},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Any alerts?",
                expected_parameters={},
            ),
            CommandExample(
                voice_command="Clear my alerts.",
                expected_parameters={"dismiss": True},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return self.generate_prompt_examples()

    # ------------------------------------------------------------------
    # Pre-routing
    # ------------------------------------------------------------------

    _FAST_PATH_ID = "check_alerts.greeting"
    _DISMISS_FAST_PATH_ID = "check_alerts.dismiss"

    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            FastPathPattern(
                id=self._FAST_PATH_ID,
                description="Bypass the LLM when greeting phrases ('what's up', 'any alerts') are spoken with pending alerts in the queue",
                example="what's up",
            ),
            FastPathPattern(
                id=self._DISMISS_FAST_PATH_ID,
                description="Bypass the LLM when dismiss phrases ('clear alerts', 'dismiss notifications') are spoken — silently flushes the queue",
                example="clear alerts",
            ),
        ]

    def pre_route(
        self,
        voice_command: str,
        *,
        disabled_pattern_ids: "set[str] | frozenset[str]" = frozenset(),
    ) -> PreRouteResult | None:
        text = voice_command.strip().lower()
        if not text:
            return None

        # Dismiss is checked first — its phrases are more specific than
        # the greeting phrases. "clear notifications" contains
        # "notifications" but isn't a "what's up" — treat it as dismiss.
        if self._DISMISS_FAST_PATH_ID not in disabled_pattern_ids:
            if _DISMISS_RE.search(text) and not _DISMISS_NEGATION_RE.search(text):
                queue = get_alert_queue_service()
                dropped = queue.flush()
                logger.info(
                    "Pre-routed check_alerts.dismiss",
                    alert_count=len(dropped),
                )
                return PreRouteResult(
                    arguments={"dismissed": True, "dismissed_count": len(dropped)},
                )

        if self._FAST_PATH_ID in disabled_pattern_ids:
            return None

        matched_phrase = next((p for p in _TRIGGER_PHRASES if p in text), None)
        if matched_phrase is not None and len(text) - len(matched_phrase) > 10:
            # "what's new with the Lakers?" is a question for the LLM, not
            # an alert check — only near-standalone greetings fast-path.
            matched_phrase = None
        matched = matched_phrase is not None
        if not matched:
            # Also check if text is a substring of any trigger
            matched = any(text in phrase for phrase in _TRIGGER_PHRASES if len(text) >= 4)

        if not matched:
            return None

        queue = get_alert_queue_service()
        if queue.count() == 0:
            # No alerts — fall through to LLM for casual "what's up" reply
            return None

        alerts = queue.flush()
        alerts_data = [a.to_dict() for a in alerts]
        logger.info("Pre-routed check_alerts", alert_count=len(alerts_data))

        return PreRouteResult(
            arguments={"alerts_json": json.dumps(alerts_data)},
        )

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    @staticmethod
    def _truthy(value: Any) -> bool:
        """Text-based tool calling can deliver booleans as strings."""
        return value in (True, 1, "1", "true", "True", "yes")

    def validate_call(self, **kwargs: Any) -> List[ValidationResult]:
        # Text-based tool calling can deliver booleans as strings; coerce
        # before the SDK's type validation so {"dismiss": "true"} doesn't
        # bounce at execute() (run() re-coerces via _truthy independently).
        if "dismiss" in kwargs:
            kwargs = {**kwargs, "dismiss": self._truthy(kwargs.get("dismiss"))}
        return super().validate_call(**kwargs)

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        # Silent-dismiss path: pre_route already flushed the queue. The
        # extra flush is idempotent (always fires on_change(0)) and also
        # covers an LLM-hallucinated {"dismissed": true} — "Cleared." must
        # never be a lie that leaves the queue populated.
        if self._truthy(kwargs.get("dismissed")) or self._truthy(kwargs.get("dismiss")):
            dropped = get_alert_queue_service().flush()
            if dropped:
                logger.info("check_alerts dismiss", alert_count=len(dropped))
            return CommandResponse.success_response(
                context_data={"message": "Cleared."},
                wait_for_input=False,
            )

        alerts_json: str = kwargs.get("alerts_json") or "[]"

        try:
            alerts_data = json.loads(alerts_json)
        except (json.JSONDecodeError, TypeError):
            alerts_data = []

        if alerts_data:
            # Pre-routed: the greeting fast-path already flushed the queue
            # and handed us the alerts to deliver.
            composed = self._compose_response(alerts_data)
            return CommandResponse.success_response(
                context_data={"message": composed},
                wait_for_input=False,
            )

        # LLM-routed with no pre-route data: read the LIVE queue.
        # Answering "No pending alerts." unconditionally here left queued
        # alerts (and a purple LED) behind — the exact "purple with no
        # alerts" interaction observed in the field.
        queue = get_alert_queue_service()
        pending = queue.get_pending()
        if not pending:
            return CommandResponse.success_response(
                context_data={"message": "No pending alerts."},
                wait_for_input=False,
            )

        composed = self._compose_response([a.to_dict() for a in pending])
        # Consume only what we read, and only after composition produced a
        # message (it has a non-LLM fallback) — a crash above must not
        # silently destroy the queue.
        queue.remove_ids([a.id for a in pending])

        return CommandResponse.success_response(
            context_data={"message": composed},
            wait_for_input=False,
        )

    def _compose_response(self, alerts_data: List[Dict[str, Any]]) -> str:
        """Send alert summaries to CC for natural spoken composition."""
        prompt = (
            "/no_think\n"
            "Deliver these updates conversationally, like a friend catching you up. "
            "Be concise — one or two sentences per alert. "
            "Group by topic if multiple alerts are from the same source.\n\n"
            f"Alerts:\n{json.dumps(alerts_data, indent=2)}\n\n"
            "Respond in 2-6 spoken sentences."
        )

        try:
            cc_url = get_command_center_url()
            client = JarvisCommandCenterClient(cc_url)
            composed = client.chat_text(prompt)
            if composed:
                composed = re.sub(r"<think>.*?</think>\s*", "", composed, flags=re.DOTALL)
                return composed.strip()
        except Exception as e:
            logger.warning("LLM composition failed for alerts", error=str(e))

        # Fallback: simple concatenation
        parts = [f"{a['title']}: {a['summary']}" for a in alerts_data if a.get("title")]
        return ". ".join(parts) if parts else "You have pending alerts."
