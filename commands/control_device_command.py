"""
Control Device command for Jarvis.

Generic device control for any Home Assistant domain.
Uses domain-based action validation with clarification flow.
"""

import asyncio
from typing import Any, Dict, List, Optional

from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from core.ijarvis_authentication import AuthenticationConfig
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation
from core.validation_result import ValidationResult
from services.home_assistant_service import (
    HomeAssistantService,
    get_action_display_name,
    get_actions_for_domain,
    get_domain_from_entity_id,
)
from utils.entity_resolver import resolve_entity_id

logger = JarvisLogger(service="jarvis-node")

# Maps voice command action verbs to domain-specific HA service actions.
# Used when entity resolution changes the domain and the original action
# is no longer valid (e.g., switch.turn_on → lock needs "lock" not "turn_on").
VOICE_VERB_TO_ACTION: dict[str, dict[str, str]] = {
    "lock": {"lock": "lock"},
    "unlock": {"lock": "unlock"},
    "open": {"cover": "open_cover"},
    "close": {"cover": "close_cover"},
    "start": {"vacuum": "start"},
    "stop": {"vacuum": "stop", "cover": "stop_cover"},
    "play": {"media_player": "media_play"},
    "pause": {"media_player": "media_pause"},
}


class ControlDeviceCommand(IJarvisCommand):
    """Command for controlling any Home Assistant device.

    Handles any controllable domain (covers, locks, climate, etc.)
    with domain-appropriate action validation.

    If action is missing or invalid, returns a validation response
    with allowed actions for that device type.
    """

    @property
    def command_name(self) -> str:
        return "control_device"

    @property
    def description(self) -> str:
        return (
            "Control a HA device: turn on/off, lock/unlock, open/close, set temperature. "
            "Use for ACTION commands (imperative verbs). "
            "Domain: lock/unlock→lock.*, open/close→cover.*, on/off→light.*/switch.*."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "turn on",
            "turn off",
            "switch on",
            "switch off",
            "open",
            "close",
            "lock",
            "unlock",
            "start",
            "stop",
            "set",
            "adjust",
            "control",
            "activate",
            "light",
            "lights",
            "scene",
            "garage",
            "door",
            "thermostat",
            "fan",
            "vacuum",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "entity_id",
                "string",
                required=True,
                description=(
                    "Entity ID from device_controls. Select by [area] tag, not name. "
                    "Verify domain matches action verb. NEVER invent entity IDs."
                ),
            ),
            JarvisParameter(
                "action",
                "string",
                required=False,
                description=(
                    "Action to perform. MUST match the entity domain. "
                    "Pick ONE: "
                    "lock.* → 'lock' or 'unlock'; "
                    "cover.* → 'open_cover' or 'close_cover' or 'stop_cover'; "
                    "light.*/switch.* → 'turn_on' or 'turn_off' or 'toggle'; "
                    "climate.* → 'set_temperature' or 'set_hvac_mode' or 'turn_on' or 'turn_off'. "
                    "If unsure, omit and system will ask."
                ),
                enum_values=[
                    "turn_on", "turn_off", "toggle",
                    "lock", "unlock",
                    "open_cover", "close_cover", "stop_cover",
                    "set_temperature", "set_hvac_mode",
                    "start", "stop", "return_to_base",
                    "set_percentage",
                    "media_play", "media_pause", "media_stop",
                    "volume_up", "volume_down",
                    "trigger",
                ],
            ),
            JarvisParameter(
                "value",
                "string",
                required=False,
                description=(
                    "Value for set actions (e.g., temperature '72' for climate, "
                    "percentage '50' for fans). Only needed for set_* actions."
                ),
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "HOME_ASSISTANT_REST_URL",
                "Home Assistant REST API URL (e.g., http://192.168.1.100:8123)",
                "integration",
                "string",
            ),
            JarvisSecret(
                "HOME_ASSISTANT_API_KEY",
                "Home Assistant long-lived access token",
                "integration",
                "string",
            ),
        ]

    @property
    def authentication(self) -> AuthenticationConfig:
        return AuthenticationConfig(
            type="oauth",
            provider="home_assistant",
            client_id="http://jarvis-node-mobile",
            keys=["access_token"],
            authorize_path="/auth/authorize",
            exchange_path="/auth/token",
            discovery_port=8123,
            discovery_probe_path="/api/",
            send_redirect_uri_in_exchange=False,
        )

    def store_auth_values(self, values: dict[str, str]) -> None:
        """Receive short-lived OAuth token, create LLAT, store HA secrets.

        The mobile app discovers HA on the LAN, runs generic OAuth,
        and pushes the short-lived token + base URL here. The node
        creates a long-lived token via WebSocket (same LAN as HA)
        and stores all credentials as secrets.
        """
        access_token = values["access_token"]
        base_url = values["_base_url"]
        ws_url = base_url.replace("http", "ws") + "/api/websocket"

        # Create long-lived token via HA WebSocket API
        llat = self._create_long_lived_token(ws_url, access_token)

        from services.secret_service import set_secret
        set_secret("HOME_ASSISTANT_REST_URL", base_url, "integration")
        set_secret("HOME_ASSISTANT_WS_URL", ws_url, "integration")
        set_secret("HOME_ASSISTANT_API_KEY", llat, "integration")

        # Clear re-auth flag
        from services.command_auth_service import clear_auth_flag
        clear_auth_flag("home_assistant")

    def _create_long_lived_token(self, ws_url: str, access_token: str) -> str:
        """Create a long-lived access token via HA WebSocket API.

        Args:
            ws_url: WebSocket URL (e.g., ws://192.168.1.100:8123/api/websocket)
            access_token: Short-lived OAuth access token

        Returns:
            Long-lived access token string

        Raises:
            RuntimeError: If token creation fails
        """
        import json
        import websocket

        ws = websocket.create_connection(ws_url)
        try:
            # Step 1: Receive auth_required message
            ws.recv()

            # Step 2: Authenticate with short-lived token
            ws.send(json.dumps({"type": "auth", "access_token": access_token}))
            auth_result = json.loads(ws.recv())
            if auth_result.get("type") != "auth_ok":
                raise RuntimeError(f"HA WebSocket auth failed: {auth_result}")

            # Step 3: Create long-lived access token
            ws.send(json.dumps({
                "id": 1,
                "type": "auth/long_lived_access_token",
                "client_name": "Jarvis Node",
                "lifespan": 365,
            }))
            result = json.loads(ws.recv())
            if not result.get("success"):
                raise RuntimeError(f"Failed to create LLAT: {result}")
            return result["result"]
        finally:
            ws.close()

    @property
    def rules(self) -> List[str]:
        return [
            "Find device by [area] tag in device_controls — match user's room name to area",
            "If user intent is clear (e.g., 'open the garage'), include the action",
            "If action is ambiguous, omit it and let system ask for clarification",
            "For temperature settings, include value parameter",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Select entity by [area] tag, NOT name similarity. 'Hue Play' ≠ 'Play room'.",
            "lock/unlock → lock.* (NEVER switch.*), open/close → cover.*, on/off → light.*/switch.*.",
            "Floor commands ('downstairs'): find ALL areas on that floor, call once per device.",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration."""
        return [
            CommandExample(
                voice_command="Turn on my office lights",
                expected_parameters={
                    "entity_id": "light.my_office",
                    "action": "turn_on",
                },
                is_primary=True,
            ),
            CommandExample(
                voice_command="Turn off the basement lights",
                expected_parameters={
                    "entity_id": "light.basement",
                    "action": "turn_off",
                },
            ),
            CommandExample(
                voice_command="Lock the front door",
                expected_parameters={
                    "entity_id": "lock.front_door",
                    "action": "lock",
                },
            ),
            CommandExample(
                voice_command="Open the garage door",
                expected_parameters={
                    "entity_id": "cover.garage_door",
                    "action": "open_cover",
                },
            ),
            CommandExample(
                voice_command="Set the thermostat to 72",
                expected_parameters={
                    "entity_id": "climate.thermostat",
                    "action": "set_temperature",
                    "value": "72",
                },
            ),
            CommandExample(
                voice_command="Turn off the lights downstairs",
                expected_parameters={
                    "entity_id": "light.living_room",
                    "action": "turn_off",
                },
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Dynamically generates examples from real HA entities when available,
        falling back to hardcoded static examples if HA is unreachable.
        """
        from utils.ha_training_data import generate_control_examples, get_ha_training_data

        ha_data = get_ha_training_data()
        if ha_data:
            dynamic = generate_control_examples(
                ha_data.get("device_controls", {}),
                ha_data.get("light_controls", {}),
            )
            if dynamic:
                return dynamic + self._generic_domain_examples(ha_data)

        return self._static_adapter_examples()

    def _generic_domain_examples(
        self, ha_data: Dict[str, Any],
    ) -> List[CommandExample]:
        """Generate examples for domains NOT present in user's HA.

        Ensures the adapter still learns patterns for cover, lock, climate, etc.
        even if the user doesn't have those device types.

        Args:
            ha_data: HA training data with device_controls

        Returns:
            List of generic examples for missing domains
        """
        device_controls = ha_data.get("device_controls", {})
        generic: List[CommandExample] = []

        domain_examples = {
            "cover": [
                ("Open the garage door", {"entity_id": "cover.garage_door", "action": "open_cover"}),
                ("Close the garage door", {"entity_id": "cover.garage_door", "action": "close_cover"}),
                ("Open the blinds", {"entity_id": "cover.blinds", "action": "open_cover"}),
            ],
            "lock": [
                ("Lock the front door", {"entity_id": "lock.front_door", "action": "lock"}),
                ("Unlock the front door", {"entity_id": "lock.front_door", "action": "unlock"}),
            ],
            "climate": [
                ("Set the thermostat to 72", {"entity_id": "climate.thermostat", "action": "set_temperature", "value": "72"}),
                ("Turn on the AC", {"entity_id": "climate.thermostat", "action": "turn_on"}),
                ("Turn off the heat", {"entity_id": "climate.thermostat", "action": "turn_off"}),
            ],
            "vacuum": [
                ("Start the vacuum", {"entity_id": "vacuum.roborock", "action": "start"}),
                ("Stop the vacuum", {"entity_id": "vacuum.roborock", "action": "stop"}),
                ("Send vacuum home", {"entity_id": "vacuum.roborock", "action": "return_to_base"}),
            ],
            "fan": [
                ("Turn on the bedroom fan", {"entity_id": "fan.bedroom", "action": "turn_on"}),
                ("Turn off the fan", {"entity_id": "fan.bedroom", "action": "turn_off"}),
            ],
        }

        for domain, examples in domain_examples.items():
            if domain not in device_controls:
                for utterance, params in examples:
                    generic.append(CommandExample(
                        voice_command=utterance,
                        expected_parameters=params,
                    ))

        return generic

    def _static_adapter_examples(self) -> List[CommandExample]:
        """Fallback static examples when HA is unreachable."""
        items = [
            # Light controls - office
            ("Turn on my office lights", {"entity_id": "light.my_office", "action": "turn_on"}),
            ("Turn off the office lights", {"entity_id": "light.my_office", "action": "turn_off"}),
            ("Lights off in my office", {"entity_id": "light.my_office", "action": "turn_off"}),
            ("Switch on the office light", {"entity_id": "light.my_office", "action": "turn_on"}),
            ("Kill the office lights", {"entity_id": "light.my_office", "action": "turn_off"}),
            # Light controls - office desk
            ("Turn on my office desk light", {"entity_id": "light.office_desk", "action": "turn_on"}),
            ("Turn off the desk light", {"entity_id": "light.office_desk", "action": "turn_off"}),
            ("Switch on the office desk lamp", {"entity_id": "light.office_desk", "action": "turn_on"}),
            # Light controls - office fan light
            ("Turn on the office fan light", {"entity_id": "light.office_fan", "action": "turn_on"}),
            ("Turn off the fan light", {"entity_id": "light.office_fan", "action": "turn_off"}),
            # Light controls - basement
            ("Turn on the basement lights", {"entity_id": "light.basement", "action": "turn_on"}),
            ("Turn off the basement lights", {"entity_id": "light.basement", "action": "turn_off"}),
            ("Kill the basement lights", {"entity_id": "light.basement", "action": "turn_off"}),
            ("Basement lights on", {"entity_id": "light.basement", "action": "turn_on"}),
            # Light controls - upstairs
            ("Turn on the upstairs lights", {"entity_id": "light.upstairs", "action": "turn_on"}),
            ("Turn off the upstairs lights", {"entity_id": "light.upstairs", "action": "turn_off"}),
            ("Switch on the upstairs lights", {"entity_id": "light.upstairs", "action": "turn_on"}),
            ("Upstairs lights off", {"entity_id": "light.upstairs", "action": "turn_off"}),
            # Light controls - bathroom
            ("Turn on the bathroom light", {"entity_id": "light.middle_bathroom", "action": "turn_on"}),
            ("Turn off the bathroom light", {"entity_id": "light.middle_bathroom", "action": "turn_off"}),
            ("Bathroom light on", {"entity_id": "light.middle_bathroom", "action": "turn_on"}),
            # Light controls - rest light
            ("Turn on the rest light", {"entity_id": "light.my_rest_light", "action": "turn_on"}),
            ("Turn off the rest light", {"entity_id": "light.my_rest_light", "action": "turn_off"}),
            # Switch controls - baby berardi timer
            ("Turn on the baby timer switch", {"entity_id": "switch.baby_berardi_timer", "action": "turn_on"}),
            ("Turn off the baby timer", {"entity_id": "switch.baby_berardi_timer", "action": "turn_off"}),
            ("Turn on the baby Berardi switch", {"entity_id": "switch.baby_berardi_timer", "action": "turn_on"}),
            ("Turn off the baby Berardi switch", {"entity_id": "switch.baby_berardi_timer", "action": "turn_off"}),
            ("Baby timer on", {"entity_id": "switch.baby_berardi_timer", "action": "turn_on"}),
            ("Baby timer off", {"entity_id": "switch.baby_berardi_timer", "action": "turn_off"}),
            # Scene activation
            ("Activate the office desk read scene", {"entity_id": "scene.office_desk_read", "action": "turn_on"}),
            ("Set the office desk to read", {"entity_id": "scene.office_desk_read", "action": "turn_on"}),
            ("Activate the basement bright scene", {"entity_id": "scene.basement_bright", "action": "turn_on"}),
            ("Set the basement to bright", {"entity_id": "scene.basement_bright", "action": "turn_on"}),
            ("Activate the office dimmed scene", {"entity_id": "scene.my_office_dimmed", "action": "turn_on"}),
            ("Set the upstairs to relax", {"entity_id": "scene.upstairs_relax", "action": "turn_on"}),
            ("Activate the bathroom nightlight", {"entity_id": "scene.middle_bathroom_nightlight", "action": "turn_on"}),
            # Cover/garage door controls
            ("Open the garage door", {"entity_id": "cover.garage_door", "action": "open_cover"}),
            ("Open the garage", {"entity_id": "cover.garage_door", "action": "open_cover"}),
            ("Close the garage door", {"entity_id": "cover.garage_door", "action": "close_cover"}),
            ("Close the garage", {"entity_id": "cover.garage_door", "action": "close_cover"}),
            ("Stop the garage door", {"entity_id": "cover.garage_door", "action": "stop_cover"}),
            ("Open the blinds", {"entity_id": "cover.blinds", "action": "open_cover"}),
            ("Close the blinds", {"entity_id": "cover.blinds", "action": "close_cover"}),
            # Lock controls
            ("Lock the front door", {"entity_id": "lock.front_door", "action": "lock"}),
            ("Unlock the front door", {"entity_id": "lock.front_door", "action": "unlock"}),
            ("Lock the back door", {"entity_id": "lock.back_door", "action": "lock"}),
            # Climate controls
            ("Set the thermostat to 72", {"entity_id": "climate.thermostat", "action": "set_temperature", "value": "72"}),
            ("Set temperature to 68", {"entity_id": "climate.thermostat", "action": "set_temperature", "value": "68"}),
            ("Turn on the AC", {"entity_id": "climate.thermostat", "action": "turn_on"}),
            ("Turn off the heat", {"entity_id": "climate.thermostat", "action": "turn_off"}),
            ("Set thermostat to cool", {"entity_id": "climate.thermostat", "action": "set_hvac_mode", "value": "cool"}),
            # Vacuum controls
            ("Start the vacuum", {"entity_id": "vacuum.roborock", "action": "start"}),
            ("Stop the vacuum", {"entity_id": "vacuum.roborock", "action": "stop"}),
            ("Send vacuum home", {"entity_id": "vacuum.roborock", "action": "return_to_base"}),
            # Ambiguous (no action - should trigger clarification)
            ("Do something with the garage", {"entity_id": "cover.garage_door"}),
            ("Control the thermostat", {"entity_id": "climate.thermostat"}),
        ]

        examples = []
        for i, (utterance, params) in enumerate(items):
            examples.append(
                CommandExample(
                    voice_command=utterance,
                    expected_parameters=params,
                    is_primary=(i == 0),
                )
            )
        return examples

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        """Execute the control device command.

        Args:
            request_info: Information about the request from JCC
            **kwargs: Parameters including 'entity_id', optional 'action', optional 'value'

        Returns:
            CommandResponse with success/failure or validation prompt
        """
        entity_id = kwargs.get("entity_id")
        action = kwargs.get("action")
        value = kwargs.get("value")

        original_entity_id = entity_id
        if entity_id:
            entity_id = resolve_entity_id(entity_id, request_info.voice_command)

        if not entity_id:
            return CommandResponse.error_response(
                error_details="Entity ID is required. Which device do you want to control?",
                context_data={"error": "missing_entity_id"},
            )

        # Get domain from entity_id
        domain = get_domain_from_entity_id(entity_id)
        if not domain:
            return CommandResponse.error_response(
                error_details=f"Invalid entity ID format: {entity_id}",
                context_data={"error": "invalid_entity_id"},
            )

        # Get allowed actions for this domain
        allowed_actions = get_actions_for_domain(domain)
        if not allowed_actions:
            return CommandResponse.error_response(
                error_details=f"Unknown device type: {domain}",
                context_data={"error": "unknown_domain", "domain": domain},
            )

        # If entity resolution changed the domain (cross-domain fix), the
        # original action may be invalid. Infer the correct action from the
        # voice command verbs.
        original_domain = get_domain_from_entity_id(original_entity_id or "") if original_entity_id else None
        if action and action not in allowed_actions and original_domain != domain:
            inferred = self._infer_action_from_voice(
                request_info.voice_command, domain,
            )
            if inferred:
                action = inferred

        # If no action provided, either auto-select (single option) or ask
        if not action:
            if len(allowed_actions) == 1:
                # Single-action domains (scene, script): auto-select
                action = allowed_actions[0]
            else:
                # Try to infer from voice command before asking
                inferred = self._infer_action_from_voice(
                    request_info.voice_command, domain,
                )
                if inferred:
                    action = inferred
                else:
                    return self._request_action_clarification(entity_id, domain, allowed_actions)

        # Validate action is allowed for this domain
        if action not in allowed_actions:
            return self._request_action_clarification(
                entity_id,
                domain,
                allowed_actions,
                invalid_action=action,
            )

        # Log what the LLM sent vs what we resolved
        resolved = entity_id != original_entity_id
        logger.info(
            "control_device executing",
            llm_entity_id=original_entity_id,
            resolved_entity_id=entity_id if resolved else "(exact match)",
            action=action,
            domain=domain,
            voice_command=request_info.voice_command,
        )

        # Execute the action
        return asyncio.run(self._execute_control(entity_id, domain, action, value))

    def _infer_action_from_voice(self, voice_command: str, domain: str) -> Optional[str]:
        """Infer the correct action from voice command verbs and target domain.

        Used when entity resolution changes the domain (cross-domain fix) and
        the LLM's original action is no longer valid, or when no action was
        provided.

        Args:
            voice_command: Original voice command text
            domain: The resolved entity domain

        Returns:
            Inferred action string or None if no match
        """
        if not voice_command:
            return None

        cmd_words = set(voice_command.lower().split())
        for verb, domain_actions in VOICE_VERB_TO_ACTION.items():
            if verb in cmd_words and domain in domain_actions:
                return domain_actions[domain]

        return None

    def _request_action_clarification(
        self,
        entity_id: str,
        domain: str,
        allowed_actions: List[str],
        invalid_action: Optional[str] = None,
    ) -> CommandResponse:
        """Return a validation response asking for action clarification.

        Args:
            entity_id: The device entity ID
            domain: Device domain
            allowed_actions: List of allowed action names
            invalid_action: The invalid action that was provided (if any)

        Returns:
            CommandResponse configured as validation prompt
        """
        # Build human-friendly action list
        action_choices = [get_action_display_name(a) for a in allowed_actions]
        action_list = ", ".join(action_choices[:-1])
        if len(action_choices) > 1:
            action_list += f", or {action_choices[-1]}"
        else:
            action_list = action_choices[0]

        if invalid_action:
            message = (
                f"'{invalid_action}' isn't valid for this device. "
                f"Would you like to {action_list}?"
            )
        else:
            message = f"What would you like to do? {action_list.capitalize()}?"

        return CommandResponse.follow_up_response(
            context_data={
                "validation_type": "action_required",
                "entity_id": entity_id,
                "domain": domain,
                "allowed_actions": allowed_actions,
                "prompt": message,
            },
        )

    def validate_call(self, **kwargs: Any) -> list[ValidationResult]:
        """Validate entity_id against known HA entities.

        Runs default enum/type checks first, then validates entity_id
        against cached HA data. Auto-corrects when unambiguous; returns
        error with alternatives when ambiguous.
        """
        results = super().validate_call(**kwargs)

        entity_id = kwargs.get("entity_id", "")
        if not entity_id:
            results.append(ValidationResult(
                success=False,
                param_name="entity_id",
                command_name=self.command_name,
                message="entity_id is required. Call get_ha_entities first.",
            ))
            return results

        known = ControlDeviceCommand._get_known_entities()
        if not known or entity_id in known:
            return results

        # Try fuzzy auto-correct
        best = self._find_best_match(entity_id, known)
        if best:
            results.append(ValidationResult(
                success=True,
                param_name="entity_id",
                command_name=self.command_name,
                suggested_value=best,
            ))
        else:
            domain = entity_id.split(".")[0] if "." in entity_id else ""
            alts = [eid for eid in known if eid.startswith(f"{domain}.")]
            alt_lines = [f"  - {eid}" for eid in alts[:20]]
            results.append(ValidationResult(
                success=False,
                param_name="entity_id",
                command_name=self.command_name,
                message=(
                    f"Entity '{entity_id}' not found. "
                    f"Valid {domain or 'all'} entities:\n" + "\n".join(alt_lines)
                    + "\nYou MUST re-call the same tool with a correct entity_id "
                    "from the list above. Do NOT answer directly."
                ),
                valid_values=alts,
            ))
        return results

    @staticmethod
    def _get_known_entities() -> dict[str, str]:
        """Get entity_id -> friendly_name map from cached HA context.

        Returns empty dict if HA data is unavailable.
        """
        try:
            from services.agent_scheduler_service import get_agent_scheduler_service
            context = get_agent_scheduler_service().get_aggregated_context()
            ha_data = context.get("home_assistant", {})
        except Exception:
            return {}

        entities: dict[str, str] = {}

        # Light controls
        for name, info in ha_data.get("light_controls", {}).items():
            eid = info.get("entity_id", "")
            if eid:
                entities[eid] = name

        # Device controls (all domains)
        for domain_devices in ha_data.get("device_controls", {}).values():
            for dev in domain_devices:
                if dev.get("state") == "unavailable":
                    continue
                eid = dev.get("entity_id", "")
                if eid and eid not in entities:
                    entities[eid] = dev.get("name", "")

        return entities

    @staticmethod
    def _find_best_match(entity_id: str, known: dict[str, str]) -> str | None:
        """Find unambiguous auto-correction for a wrong entity_id.

        Returns corrected entity_id if there's exactly one strong match
        in the same domain, or None if ambiguous/no match.
        """
        if "." not in entity_id:
            return None

        domain_prefix = entity_id.split(".")[0]
        guessed_slug = entity_id.split(".", 1)[1]
        guessed_words = set(guessed_slug.split("_"))

        candidates: list[tuple[str, int]] = []
        for eid in known:
            if not eid.startswith(f"{domain_prefix}."):
                continue
            known_slug = eid.split(".", 1)[1] if "." in eid else eid

            # Containment check
            if known_slug in guessed_slug or guessed_slug in known_slug:
                candidates.append((eid, 2))
                continue

            # Word overlap
            known_words = set(known_slug.split("_"))
            overlap = len(guessed_words & known_words)
            if overlap > 0:
                candidates.append((eid, overlap))

        if not candidates:
            return None

        candidates.sort(key=lambda c: c[1], reverse=True)
        if len(candidates) == 1:
            return candidates[0][0]
        if candidates[0][1] > candidates[1][1]:
            return candidates[0][0]

        return None

    async def _execute_control(
        self,
        entity_id: str,
        domain: str,
        action: str,
        value: Optional[str],
    ) -> CommandResponse:
        """Execute the device control via HomeAssistantService.

        Args:
            entity_id: HA entity ID
            domain: Device domain
            action: Action to perform
            value: Optional value for set actions

        Returns:
            CommandResponse with result
        """
        try:
            service = HomeAssistantService()

            # Build service data if value provided
            data: Optional[Dict[str, Any]] = None
            if value is not None:
                if action == "set_temperature":
                    try:
                        data = {"temperature": float(value)}
                    except ValueError:
                        return CommandResponse.error_response(
                            error_details=f"Invalid temperature value: {value}",
                            context_data={"error": "invalid_value"},
                        )
                elif action == "set_hvac_mode":
                    data = {"hvac_mode": value}
                elif action == "set_percentage":
                    try:
                        data = {"percentage": int(value)}
                    except ValueError:
                        return CommandResponse.error_response(
                            error_details=f"Invalid percentage value: {value}",
                            context_data={"error": "invalid_value"},
                        )

            result = await service.control_device(entity_id, action, data)

            if result.success:
                friendly_action = get_action_display_name(action)
                return CommandResponse.success_response(
                    context_data={
                        "entity_id": entity_id,
                        "domain": domain,
                        "action": action,
                        "value": value,
                        "message": f"Successfully executed {friendly_action} on {entity_id}",
                    },
                    wait_for_input=False,
                )
            else:
                return CommandResponse.error_response(
                    error_details=f"Failed to control device: {result.error}",
                    context_data={"error": result.error},
                )

        except ValueError as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"error": "configuration_error"},
            )
