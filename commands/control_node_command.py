"""Control the speaker node itself: volume up/down, set volume, mute/unmute.

Distinct from ``control_device``, which targets smart-home gear via the
device-protocol layer. ``control_node`` only touches local hardware
(today: audio output via ALSA + PulseAudio).

The pre-route here covers the common verb+phrasing intents
("volume up", "set volume to 5", "mute") so they bypass the LLM —
deterministic regex is faster and more reliable for this shape of
intent than asking the LLM to pick the right action enum.
"""

import re
from typing import List

from jarvis_command_sdk import (
    CommandExample,
    FastPathPattern,
    IJarvisCommand,
    PreRouteResult,
)

from core.command_response import CommandResponse
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from utils.audio_volume import (
    adjust_volume_percent,
    set_muted,
    set_volume_percent,
)


# --- Pre-route patterns ---

_VOLUME_UP_RE = re.compile(
    r"\b(?:turn\s+(?:the\s+)?)?volume\s+up\b|\b(?:turn\s+it\s+up|louder|crank\s+it\s+up)\b",
    re.IGNORECASE,
)
_VOLUME_DOWN_RE = re.compile(
    r"\b(?:turn\s+(?:the\s+)?)?volume\s+down\b|\b(?:turn\s+it\s+down|quieter|softer)\b",
    re.IGNORECASE,
)
# "set the volume to 50%" / "change volume to 5" / "make the volume 70" — the
# preposition/verb anchors the match so we don't fire on "loud volume 5 wine".
_VOLUME_SET_RE = re.compile(
    r"\b(?:set|change|make|put)\s+(?:the\s+)?volume\s+(?:to\s+|at\s+)?(\d+)\s*(%)?",
    re.IGNORECASE,
)
# Bare "volume 5" / "volume 50%" — runs only after the up/down patterns
# fail, so "volume up" never matches the digit branch.
_VOLUME_BARE_RE = re.compile(
    r"\bvolume\s+(\d+)\s*(%)?\b",
    re.IGNORECASE,
)
_MUTE_RE = re.compile(
    r"^\s*(?:please\s+)?mute(?:\s+(?:the\s+)?(?:volume|sound|audio|speaker))?\s*\.?$",
    re.IGNORECASE,
)
_UNMUTE_RE = re.compile(
    r"^\s*(?:please\s+)?unmute(?:\s+(?:the\s+)?(?:volume|sound|audio|speaker))?\s*\.?$",
    re.IGNORECASE,
)

_STEP_DEFAULT = 10  # percentage points per "volume up" / "volume down"
_BARE_SCALE_THRESHOLD = 10  # bare numbers ≤ this → 0-10 scale; above → literal %


def _interpret_target(value: int, has_percent: bool) -> int:
    """Map a parsed volume token to an absolute 0-100 percent.

    Sonos/Alexa-style scale:
      - explicit "%" → literal percent
      - bare number ≤ 10 → 0-10 scale × 10 (5 → 50%, 10 → 100%)
      - bare number > 10 → literal percent
    """
    if has_percent:
        return max(0, min(100, value))
    if value <= _BARE_SCALE_THRESHOLD:
        return max(0, min(100, value * 10))
    return max(0, min(100, value))


class ControlNodeCommand(IJarvisCommand):
    """Volume + mute control for the speaker node.

    Designed to grow into other node-level controls (LED, identify,
    restart) without redesign — new actions extend the ``action`` enum.
    """

    @property
    def command_name(self) -> str:
        return "control_node"

    @property
    def description(self) -> str:
        return (
            "Control the speaker node itself: volume up/down, set volume to "
            "a percent, or mute/unmute. Use this for the device's own audio "
            "output, NOT for smart-home devices (use control_device for those)."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "volume", "louder", "quieter", "softer", "mute", "unmute",
            "turn up", "turn down", "turn it up", "turn it down",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "action",
                "string",
                required=True,
                description=(
                    "What to do: 'volume_up' (relative +), 'volume_down' "
                    "(relative -), 'set_volume' (absolute), 'mute', or 'unmute'."
                ),
                enum_values=["volume_up", "volume_down", "set_volume", "mute", "unmute"],
            ),
            JarvisParameter(
                "target_percent",
                "int",
                required=False,
                description=(
                    "Required only when action='set_volume'. Absolute volume "
                    "as 0-100 percent. If the user said a 0-10 number "
                    "without '%', multiply by 10 (5 → 50)."
                ),
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def critical_rules(self) -> List[str]:
        return [
            "target_percent must be 0-100 when action='set_volume'.",
            "Treat 'volume up/down/louder/quieter' as relative — pick volume_up or volume_down, NOT set_volume.",
            "Map 'mute/silence the speaker' → action='mute'; 'unmute' → action='unmute'.",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Turn the volume up",
                expected_parameters={"action": "volume_up"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Volume down",
                expected_parameters={"action": "volume_down"},
            ),
            CommandExample(
                voice_command="Set the volume to 50%",
                expected_parameters={"action": "set_volume", "target_percent": 50},
            ),
            CommandExample(
                voice_command="Set the volume to 5",
                expected_parameters={"action": "set_volume", "target_percent": 50},
            ),
            CommandExample(
                voice_command="Mute",
                expected_parameters={"action": "mute"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items: list[tuple[str, dict[str, object]]] = [
            # Up
            ("Turn the volume up", {"action": "volume_up"}),
            ("Volume up", {"action": "volume_up"}),
            ("Louder", {"action": "volume_up"}),
            ("Turn it up", {"action": "volume_up"}),
            # Down
            ("Turn the volume down", {"action": "volume_down"}),
            ("Volume down", {"action": "volume_down"}),
            ("Quieter", {"action": "volume_down"}),
            ("Softer please", {"action": "volume_down"}),
            # Set — explicit percent
            ("Set the volume to 50%", {"action": "set_volume", "target_percent": 50}),
            ("Change volume to 30%", {"action": "set_volume", "target_percent": 30}),
            # Set — bare > 10 (literal percent)
            ("Set the volume to 50", {"action": "set_volume", "target_percent": 50}),
            ("Set volume to 75", {"action": "set_volume", "target_percent": 75}),
            # Set — bare ≤ 10 (0-10 scale)
            ("Set the volume to 5", {"action": "set_volume", "target_percent": 50}),
            ("Volume 7", {"action": "set_volume", "target_percent": 70}),
            ("Set volume to 3", {"action": "set_volume", "target_percent": 30}),
            # Mute / unmute
            ("Mute", {"action": "mute"}),
            ("Mute the volume", {"action": "mute"}),
            ("Unmute", {"action": "unmute"}),
            ("Unmute the speaker", {"action": "unmute"}),
        ]
        return [
            CommandExample(
                voice_command=utterance,
                expected_parameters=params,
                is_primary=(i == 0),
            )
            for i, (utterance, params) in enumerate(items)
        ]

    # ------------------------------------------------------------------
    # Pre-routing — bypasses LLM for the common verb+phrasing intents.
    # Migrated to declarative fast_path_patterns; the SDK's default
    # pre_route() iterates these in order and dispatches to the handler.
    # ------------------------------------------------------------------
    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        # Order matters — set-with-verb runs before bare so "set volume to 5"
        # doesn't get caught by the bare-number pattern, and up/down run
        # before bare so "volume up" can't be misread as a digit.
        return [
            FastPathPattern(
                id="control_node.mute",
                description="Bypass LLM for 'mute' / 'mute the speaker'",
                example="mute",
                regex=_MUTE_RE.pattern,
                handler="_fp_mute",
            ),
            FastPathPattern(
                id="control_node.unmute",
                description="Bypass LLM for 'unmute' / 'unmute the speaker'",
                example="unmute",
                regex=_UNMUTE_RE.pattern,
                handler="_fp_unmute",
            ),
            FastPathPattern(
                id="control_node.volume_set",
                description="Bypass LLM for 'set volume to N' / 'change volume to N%'",
                example="set the volume to 50%",
                regex=_VOLUME_SET_RE.pattern,
                handler="_fp_volume_set",
            ),
            FastPathPattern(
                id="control_node.volume_up",
                description="Bypass LLM for 'volume up' / 'louder' / 'turn it up'",
                example="volume up",
                regex=_VOLUME_UP_RE.pattern,
                handler="_fp_volume_up",
            ),
            FastPathPattern(
                id="control_node.volume_down",
                description="Bypass LLM for 'volume down' / 'quieter' / 'turn it down'",
                example="volume down",
                regex=_VOLUME_DOWN_RE.pattern,
                handler="_fp_volume_down",
            ),
            FastPathPattern(
                id="control_node.volume_bare",
                description="Bypass LLM for bare 'volume N' / 'volume N%' (no verb)",
                example="volume 7",
                regex=_VOLUME_BARE_RE.pattern,
                handler="_fp_volume_bare",
            ),
        ]

    def _fp_mute(self, match, voice_command):
        return PreRouteResult(arguments={"action": "mute"})

    def _fp_unmute(self, match, voice_command):
        return PreRouteResult(arguments={"action": "unmute"})

    def _fp_volume_set(self, match, voice_command):
        target = _interpret_target(int(match.group(1)), bool(match.group(2)))
        return PreRouteResult(
            arguments={"action": "set_volume", "target_percent": target}
        )

    def _fp_volume_up(self, match, voice_command):
        return PreRouteResult(arguments={"action": "volume_up"})

    def _fp_volume_down(self, match, voice_command):
        return PreRouteResult(arguments={"action": "volume_down"})

    def _fp_volume_bare(self, match, voice_command):
        target = _interpret_target(int(match.group(1)), bool(match.group(2)))
        return PreRouteResult(
            arguments={"action": "set_volume", "target_percent": target}
        )

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        action = (kwargs.get("action") or "").strip().lower()

        if action in ("volume_up", "volume_down"):
            delta = _STEP_DEFAULT if action == "volume_up" else -_STEP_DEFAULT
            new = adjust_volume_percent(delta)
            if new is None:
                return CommandResponse.error_response(
                    error_details="Could not read current volume.",
                    context_data={"action": action},
                )
            return CommandResponse.success_response(
                context_data={
                    "action": action,
                    "volume_percent": new,
                    "message": f"Volume {new} percent.",
                },
                wait_for_input=False,
            )

        if action == "set_volume":
            raw = kwargs.get("target_percent")
            if raw is None:
                return CommandResponse.error_response(
                    error_details="target_percent is required for set_volume",
                    context_data={"action": action, "parameters_received": kwargs},
                )
            try:
                target = int(raw)
            except (TypeError, ValueError):
                return CommandResponse.error_response(
                    error_details=f"target_percent must be an integer, got {raw!r}",
                    context_data={"action": action, "parameters_received": kwargs},
                )
            target = max(0, min(100, target))
            if not set_volume_percent(target):
                return CommandResponse.error_response(
                    error_details="Failed to set volume.",
                    context_data={"action": action, "target_percent": target},
                )
            return CommandResponse.success_response(
                context_data={
                    "action": action,
                    "volume_percent": target,
                    "message": f"Volume set to {target} percent.",
                },
                wait_for_input=False,
            )

        if action in ("mute", "unmute"):
            muted = action == "mute"
            if not set_muted(muted):
                return CommandResponse.error_response(
                    error_details=f"Failed to {action}.",
                    context_data={"action": action},
                )
            return CommandResponse.success_response(
                context_data={
                    "action": action,
                    "message": "Muted." if muted else "Unmuted.",
                },
                wait_for_input=False,
            )

        return CommandResponse.error_response(
            error_details=f"Unknown action: {action}",
            context_data={"action": action, "parameters_received": kwargs},
        )
