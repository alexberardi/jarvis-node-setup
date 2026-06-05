"""WhatsUpCommand — deliver or silently dismiss pending alerts via voice.

Two fast-path patterns:

* ``check_alerts.greeting`` — "what's up", "any alerts" etc. flush the
  queue and ANNOUNCE the alerts via CC composition.
* ``check_alerts.dismiss`` — "clear alerts", "dismiss notifications"
  etc. flush the queue SILENTLY (just a brief "Cleared." reply) so the
  user can drop the alert-pending LED without sitting through every
  announcement.

If no alerts and the greeting fast-path triggers, returns None so the
LLM handles it as casual conversation. The dismiss fast-path always
matches — clearing an empty queue is a valid intent.
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
    "any alerts",
    "any updates",
    "anything new",
    "what did i miss",
    "any notifications",
    "check alerts",
    "check notifications",
]

_DISMISS_PHRASES = [
    "clear alerts",
    "clear all alerts",
    "clear notifications",
    "clear all notifications",
    "dismiss alerts",
    "dismiss all alerts",
    "dismiss notifications",
    "dismiss all notifications",
    "clear the alerts",
    "clear the notifications",
    "cancel alerts",
    "cancel notifications",
]


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
            dismiss_matched = any(phrase in text for phrase in _DISMISS_PHRASES)
            if dismiss_matched:
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

        matched = any(phrase in text for phrase in _TRIGGER_PHRASES)
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

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        # Silent-dismiss path: pre_route already flushed the queue; this
        # just confirms the action with a one-word reply so the user
        # knows the dismiss was accepted (and the LED returned to idle).
        if kwargs.get("dismissed"):
            return CommandResponse.success_response(
                context_data={"message": "Cleared."},
                wait_for_input=False,
            )

        alerts_json: str = kwargs.get("alerts_json", "[]")

        try:
            alerts_data = json.loads(alerts_json)
        except (json.JSONDecodeError, TypeError):
            alerts_data = []

        if not alerts_data:
            return CommandResponse.success_response(
                context_data={"message": "No pending alerts."},
                wait_for_input=False,
            )

        # Compose via LLM
        composed = self._compose_response(alerts_data)

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
