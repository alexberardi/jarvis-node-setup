"""WhatsUpCommand — deliver pending alerts via voice.

Pre-routes "what's up", "any alerts", etc. If alerts are pending, flushes
the queue and sends summaries to CC's chat_text() for natural composition.
If no alerts, returns None so the LLM handles it as casual conversation.
"""

import json
import re
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from clients.jarvis_command_center_client import JarvisCommandCenterClient
from core.command_response import CommandResponse
from jarvis_command_sdk import (
    CommandExample,
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

    def pre_route(self, voice_command: str) -> PreRouteResult | None:
        text = voice_command.strip().lower()
        if not text:
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
