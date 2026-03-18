"""
Control Device command for Jarvis.

Generic device control for any smart home device (Home Assistant or direct WiFi).
Uses domain-based action validation with clarification flow.
Supports floor/area targeting for multi-device commands.

Direct WiFi devices (source="direct") are controlled via LAN protocol adapters
(LIFX, TP-Link Kasa, etc.) without requiring Home Assistant.
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
from services.home_assistant_service import (
    HomeAssistantService,
    get_action_display_name,
    get_actions_for_domain,
    get_domain_from_entity_id,
)
from utils.entity_resolver import resolve_entity_id, validate_entity

# Voice keywords → HA domain for floor/area commands
_VOICE_DOMAIN_HINTS: Dict[str, str] = {
    "light": "light", "lights": "light", "lamp": "light", "lamps": "light",
    "switch": "switch", "switches": "switch",
    "lock": "lock", "locks": "lock",
    "fan": "fan", "fans": "fan",
    "cover": "cover", "blinds": "cover", "garage": "cover", "shade": "cover", "shades": "cover",
}

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
            "Control a smart home device: turn on/off, lock/unlock, open/close, set temperature. "
            "Supports HA devices and direct WiFi devices (LIFX, Kasa, etc.). "
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
                required=False,
                description=(
                    "EXACT entity_id from device_controls or light_controls context. "
                    "Format is ALWAYS 'domain.name' (e.g., 'light.my_office', 'switch.baby_berardi_timer'). "
                    "MUST include the domain prefix (light., switch., cover., lock., etc.). "
                    "Copy the entity_id exactly as shown — NEVER invent or guess entity IDs. "
                    "Required unless 'floor' or 'area' is provided."
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
                    "trigger",
                ],
            ),
            JarvisParameter(
                "floor",
                "string",
                required=False,
                description=(
                    "Floor name (e.g., 'Downstairs', 'Upstairs') to target ALL devices on that floor. "
                    "Use instead of entity_id for commands like 'turn off the lights downstairs'."
                ),
            ),
            JarvisParameter(
                "area",
                "string",
                required=False,
                description=(
                    "Area/room name (e.g., 'Living Room', 'Kitchen') to target ALL devices in that area. "
                    "Use instead of entity_id for commands like 'turn off the kitchen lights'."
                ),
            ),
            JarvisParameter(
                "room",
                "string",
                required=False,
                description=(
                    "Jarvis room name (e.g., 'Upstairs', 'Bedroom 1') from the room hierarchy. "
                    "When a room has sub-rooms, targets ALL devices in the room AND its descendants. "
                    "Use instead of entity_id for commands like 'turn off the lights upstairs'."
                ),
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
                is_sensitive=False,
                friendly_name="REST URL",
            ),
            JarvisSecret(
                "HOME_ASSISTANT_API_KEY",
                "Home Assistant long-lived access token",
                "integration",
                "string",
                friendly_name="API Key",
            ),
        ]

    @property
    def authentication(self) -> AuthenticationConfig:
        return AuthenticationConfig(
            type="oauth",
            provider="home_assistant",
            friendly_name="Home Assistant",
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
        # HA returns unknown_error if a token with the same client_name exists,
        # so we include the node_id to make names unique across nodes.
        from utils.config_service import Config
        node_suffix = (Config.get_str("node_id", "") or "")[:8]
        client_name = f"Jarvis Node {node_suffix}" if node_suffix else "Jarvis Node"
        llat = self._create_long_lived_token(ws_url, access_token, client_name=client_name)

        from services.secret_service import set_secret
        set_secret("HOME_ASSISTANT_REST_URL", base_url, "integration")
        set_secret("HOME_ASSISTANT_WS_URL", ws_url, "integration")
        set_secret("HOME_ASSISTANT_API_KEY", llat, "integration")

        # Clear re-auth flag
        from services.command_auth_service import clear_auth_flag
        clear_auth_flag("home_assistant")

    def _create_long_lived_token(
        self, ws_url: str, access_token: str, client_name: str = "Jarvis Node"
    ) -> str:
        """Create a long-lived access token via HA WebSocket API.

        Args:
            ws_url: WebSocket URL (e.g., ws://192.168.1.100:8123/api/websocket)
            access_token: Short-lived OAuth access token
            client_name: Name for the token in HA (must be unique per user)

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
                "client_name": client_name,
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
            "Floor commands ('turn off lights downstairs'): use floor param, NOT entity_id. System resolves all devices.",
            "Area commands ('turn off kitchen lights'): use area param, NOT entity_id. System resolves all devices.",
            "Room hierarchy commands ('turn off lights upstairs'): if Room Hierarchy is in context, use room param. System resolves room + all sub-rooms.",
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
                    "floor": "Downstairs",
                    "action": "turn_off",
                },
            ),
            CommandExample(
                voice_command="Turn on the kitchen lights",
                expected_parameters={
                    "area": "Kitchen",
                    "action": "turn_on",
                },
            ),
            CommandExample(
                voice_command="Turn off the lights upstairs",
                expected_parameters={
                    "room": "Upstairs",
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
            # Floor/area commands
            ("Turn off the lights downstairs", {"floor": "Downstairs", "action": "turn_off"}),
            ("Turn on the lights downstairs", {"floor": "Downstairs", "action": "turn_on"}),
            ("Turn off the lights upstairs", {"floor": "Upstairs", "action": "turn_off"}),
            ("Turn on all the lights upstairs", {"floor": "Upstairs", "action": "turn_on"}),
            ("Turn off the kitchen lights", {"area": "Kitchen", "action": "turn_off"}),
            ("Turn on the living room lights", {"area": "Living Room", "action": "turn_on"}),
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
            **kwargs: Parameters including 'entity_id', optional 'action', optional 'value',
                      optional 'floor', optional 'area'

        Returns:
            CommandResponse with success/failure or validation prompt
        """
        entity_id = kwargs.get("entity_id")
        action = kwargs.get("action")
        value = kwargs.get("value")
        floor = kwargs.get("floor")
        area = kwargs.get("area")
        room = kwargs.get("room")

        # Room hierarchy targeting — resolve room + descendants to room IDs,
        # then map to HA areas for device lookup
        if room and not entity_id:
            return self._run_room_hierarchy(
                request_info=request_info,
                room_name=room,
                action=action,
                value=value,
            )

        # Floor/area targeting — resolve to multiple entities and control all
        if (floor or area) and not entity_id:
            return self._run_floor_area(
                request_info=request_info,
                floor=floor,
                area=area,
                action=action,
                value=value,
            )

        if not entity_id:
            return CommandResponse.error_response(
                error_details="Entity ID is required. Which device do you want to control?",
                context_data={"error": "missing_entity_id"},
            )

        # Normalize: if LLM omitted domain prefix, add it based on the
        # action verb (turn_on/turn_off → light/switch, lock → lock, etc.)
        original_entity_id = entity_id
        if "." not in entity_id:
            # Replace spaces with underscores for HA entity format
            slug = entity_id.strip().replace(" ", "_").lower()
            # Infer domain from action or default to light
            domain_guess = "light"
            if action in ("lock", "unlock"):
                domain_guess = "lock"
            elif action in ("open_cover", "close_cover", "stop_cover"):
                domain_guess = "cover"
            elif action in ("set_temperature", "set_hvac_mode"):
                domain_guess = "climate"
            entity_id = f"{domain_guess}.{slug}"
            logger.info("Added missing domain prefix", original=original_entity_id, normalized=entity_id)

        # Run fuzzy resolution (handles near-misses like light.office → light.my_office)
        entity_id = resolve_entity_id(entity_id, request_info.voice_command)

        # Validate the resolved entity actually exists
        exists, _room_grouped = validate_entity(entity_id)
        if not exists:
            return CommandResponse.error_response(
                error_details=(
                    f"I couldn't find a device matching '{original_entity_id}' "
                    "in Home Assistant."
                ),
                context_data={
                    "error": "entity_not_found",
                    "invalid_entity_id": entity_id,
                },
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

    def _run_floor_area(
        self,
        request_info: RequestInformation,
        floor: Optional[str],
        area: Optional[str],
        action: Optional[str],
        value: Optional[str],
    ) -> CommandResponse:
        """Control all matching devices on a floor or in an area.

        Resolves floor → areas → entities, then controls each one.
        Defaults to light domain if not inferable from the voice command.
        """
        # Get HA context from agent scheduler
        try:
            from services.agent_scheduler_service import get_agent_scheduler_service
            context = get_agent_scheduler_service().get_aggregated_context()
            ha_data = context.get("home_assistant", {})
        except Exception as e:
            logger.error("Failed to get HA context for floor/area command", error=str(e))
            return CommandResponse.error_response(
                error_details="Home Assistant data not available",
                context_data={"error": "no_ha_context"},
            )

        if not ha_data:
            return CommandResponse.error_response(
                error_details="No Home Assistant data cached yet",
                context_data={"error": "no_ha_data"},
            )

        # Resolve floor → set of allowed area names
        floors_map: Dict[str, List[str]] = ha_data.get("floors", {})
        allowed_areas: set[str] | None = None

        if floor:
            floor_lower = floor.lower()
            for floor_name, area_list in floors_map.items():
                if floor_name.lower() == floor_lower:
                    allowed_areas = {a.lower() for a in area_list}
                    break
            if allowed_areas is None:
                return CommandResponse.error_response(
                    error_details=f"Floor '{floor}' not found. Available: {', '.join(floors_map.keys())}",
                    context_data={"error": "floor_not_found", "available_floors": list(floors_map.keys())},
                )

        if area:
            area_lower = {area.lower()}
            allowed_areas = area_lower if allowed_areas is None else allowed_areas & area_lower

        target_label = floor or area or "unknown"

        # If allowed_areas is still None (shouldn't happen), use empty set
        if allowed_areas is None:
            return CommandResponse.error_response(
                error_details="No floor or area specified",
                context_data={"error": "no_target"},
            )

        return self._run_floor_area_with_areas(
            request_info=request_info,
            ha_data=ha_data,
            allowed_areas=allowed_areas,
            target_label=target_label,
            action=action,
            value=value,
        )

    def _run_room_hierarchy(
        self,
        request_info: RequestInformation,
        room_name: str,
        action: Optional[str],
        value: Optional[str],
    ) -> CommandResponse:
        """Control all devices in a Jarvis room and its descendant rooms.

        Fetches room hierarchy from CC, resolves room + descendants to area
        names, then collects matching HA entities.
        """
        # Get HA context from agent scheduler
        try:
            from services.agent_scheduler_service import get_agent_scheduler_service
            context = get_agent_scheduler_service().get_aggregated_context()
            ha_data = context.get("home_assistant", {})
        except Exception as e:
            logger.error("Failed to get context for room hierarchy command", error=str(e))
            return CommandResponse.error_response(
                error_details="Smart home data not available",
                context_data={"error": "no_context"},
            )

        # Fetch room hierarchy from CC
        room_hierarchy = self._fetch_room_hierarchy()

        if not room_hierarchy:
            # Fallback: treat room param as an area param
            logger.info("No room hierarchy, falling back to area", room=room_name)
            return self._run_floor_area(
                request_info=request_info,
                floor=None,
                area=room_name,
                action=action,
                value=value,
            )

        # Find the target room by name (case-insensitive)
        target_room = None
        for r in room_hierarchy:
            if r["name"].lower() == room_name.lower():
                target_room = r
                break

        if not target_room:
            available = [r["name"] for r in room_hierarchy]
            return CommandResponse.error_response(
                error_details=f"Room '{room_name}' not found. Available: {', '.join(available)}",
                context_data={"error": "room_not_found", "available_rooms": available},
            )

        # BFS to collect target room + all descendant room names
        target_id = target_room["id"]
        collected_ids: set[str] = {target_id}
        queue = [target_id]
        while queue:
            parent_id = queue.pop(0)
            for r in room_hierarchy:
                if r.get("parent_room_id") == parent_id and r["id"] not in collected_ids:
                    collected_ids.add(r["id"])
                    queue.append(r["id"])

        # Map room IDs → room names (these become the allowed areas)
        room_id_to_name = {r["id"]: r["name"] for r in room_hierarchy}
        allowed_area_names: set[str] = {
            room_id_to_name[rid].lower() for rid in collected_ids if rid in room_id_to_name
        }

        logger.info(
            "Room hierarchy resolved",
            room=room_name, descendant_count=len(collected_ids),
            areas=list(allowed_area_names),
        )

        # Delegate to floor/area logic using the resolved set of area names
        # We pass area=None and inject the allowed_areas directly
        return self._run_floor_area_with_areas(
            request_info=request_info,
            ha_data=ha_data,
            allowed_areas=allowed_area_names,
            target_label=room_name,
            action=action,
            value=value,
        )

    def _run_floor_area_with_areas(
        self,
        request_info: RequestInformation,
        ha_data: Dict[str, Any],
        allowed_areas: set[str],
        target_label: str,
        action: Optional[str],
        value: Optional[str],
    ) -> CommandResponse:
        """Shared logic for controlling devices across a set of areas.

        Extracted from _run_floor_area to share with _run_room_hierarchy.
        """
        if not ha_data:
            return CommandResponse.error_response(
                error_details="No Home Assistant data cached yet",
                context_data={"error": "no_ha_data"},
            )

        # Infer domain from voice command
        domain = self._infer_domain_from_voice(request_info.voice_command)

        # Infer action from voice if not provided
        if not action:
            action = self._infer_action_from_voice(request_info.voice_command, domain)
        if not action:
            action = "turn_off" if "off" in request_info.voice_command.lower() else "turn_on"

        # Collect matching entities
        entity_ids: list[str] = []
        light_controls: Dict[str, Any] = ha_data.get("light_controls", {})
        device_controls: Dict[str, Any] = ha_data.get("device_controls", {})

        if domain == "light":
            room_group_ids: set[str] = set()
            for _lc_name, lc_info in light_controls.items():
                lc_area = lc_info.get("area", "")
                if lc_area.lower() in allowed_areas:
                    eid = lc_info.get("entity_id", "")
                    if eid:
                        entity_ids.append(eid)
                        room_group_ids.add(eid)

            for dev in device_controls.get("light", []):
                dev_area = dev.get("area", "")
                eid = dev.get("entity_id", "")
                if eid in room_group_ids or dev.get("state") == "unavailable":
                    continue
                if dev_area.lower() in allowed_areas and eid:
                    entity_ids.append(eid)
        else:
            for dev in device_controls.get(domain, []):
                dev_area = dev.get("area", "")
                if dev.get("state") == "unavailable":
                    continue
                if dev_area.lower() in allowed_areas:
                    eid = dev.get("entity_id", "")
                    if eid:
                        entity_ids.append(eid)

        if not entity_ids:
            return CommandResponse.error_response(
                error_details=f"No {domain} devices found in {target_label}",
                context_data={"error": "no_devices", "target": target_label, "domain": domain},
            )

        logger.info(
            "Room/area control",
            target=target_label, domain=domain,
            action=action, entity_count=len(entity_ids),
            entities=entity_ids,
        )

        results = asyncio.run(self._execute_multi(entity_ids, domain, action, value))

        succeeded = [r for r in results if r["success"]]
        failed = [r for r in results if not r["success"]]
        friendly_action = get_action_display_name(action)

        if succeeded and not failed:
            return CommandResponse.success_response(
                context_data={
                    "room": target_label,
                    "domain": domain,
                    "action": action,
                    "device_count": len(succeeded),
                    "entities": [r["entity_id"] for r in succeeded],
                    "message": f"Successfully {friendly_action} {len(succeeded)} {domain}(s) in {target_label}",
                },
                wait_for_input=False,
            )
        elif succeeded:
            return CommandResponse.success_response(
                context_data={
                    "room": target_label,
                    "domain": domain,
                    "action": action,
                    "device_count": len(succeeded),
                    "failed_count": len(failed),
                    "entities": [r["entity_id"] for r in succeeded],
                    "message": (
                        f"{friendly_action.capitalize()} {len(succeeded)} {domain}(s) in {target_label}, "
                        f"but {len(failed)} failed"
                    ),
                },
                wait_for_input=False,
            )
        else:
            return CommandResponse.error_response(
                error_details=f"Failed to {friendly_action} any devices in {target_label}",
                context_data={"error": "all_failed", "failed": [r["entity_id"] for r in failed]},
            )

    async def _execute_multi(
        self,
        entity_ids: list[str],
        domain: str,
        action: str,
        value: Optional[str],
    ) -> list[Dict[str, Any]]:
        """Execute an action on multiple entities concurrently.

        Routes each entity to either DirectDeviceService or HomeAssistantService
        based on whether it's a directly-controlled WiFi device.

        Returns:
            List of dicts with entity_id and success status.
        """
        ha_service = HomeAssistantService()
        direct_svc = _get_direct_device_service()

        data: Optional[Dict[str, Any]] = None
        if value is not None:
            if action == "set_temperature":
                try:
                    data = {"temperature": float(value)}
                except ValueError:
                    pass
            elif action == "set_hvac_mode":
                data = {"hvac_mode": value}
            elif action == "set_percentage":
                try:
                    data = {"percentage": int(value)}
                except ValueError:
                    pass

        async def _control_one(eid: str) -> Dict[str, Any]:
            try:
                # Route to direct service if applicable
                if direct_svc and direct_svc.is_direct_device(eid):
                    result = await direct_svc.control_device(eid, action, data)
                    return {"entity_id": eid, "success": result.success, "error": result.error}
                # Fall back to Home Assistant
                result = await ha_service.control_device(eid, action, data)
                return {"entity_id": eid, "success": result.success, "error": result.error}
            except Exception as e:
                return {"entity_id": eid, "success": False, "error": str(e)}

        return await asyncio.gather(*[_control_one(eid) for eid in entity_ids])

    @staticmethod
    def _fetch_room_hierarchy() -> List[Dict[str, Any]]:
        """Fetch room list from CC to get parent_room_id hierarchy.

        Returns list of room dicts with id, name, parent_room_id, or
        empty list if unavailable.
        """
        try:
            import httpx
            from utils.config_service import Config

            cc_url = Config.get_str("command_center_url", "")
            node_id = Config.get_str("node_id", "")
            api_key = Config.get_str("api_key", "")
            household_id = Config.get_str("household_id", "")

            if not cc_url or not household_id:
                return []

            headers = {"X-API-Key": f"{node_id}:{api_key}"} if node_id and api_key else {}
            resp = httpx.get(
                f"{cc_url}/api/v0/households/{household_id}/rooms",
                headers=headers,
                timeout=5.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception as e:
            logger.warning("Failed to fetch room hierarchy from CC", error=str(e))
        return []

    @staticmethod
    def _infer_domain_from_voice(voice_command: str) -> str:
        """Infer the HA domain from voice command keywords.

        Defaults to 'light' which covers the vast majority of floor/area commands.
        """
        words = voice_command.lower().split()
        for word in words:
            if word in _VOICE_DOMAIN_HINTS:
                return _VOICE_DOMAIN_HINTS[word]
        return "light"

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

    async def _execute_control(
        self,
        entity_id: str,
        domain: str,
        action: str,
        value: Optional[str],
    ) -> CommandResponse:
        """Execute device control via direct protocol or Home Assistant.

        Checks if the entity is a directly-controlled WiFi device first;
        if so, routes to the DirectDeviceService. Otherwise falls back
        to HomeAssistantService.

        Args:
            entity_id: Device entity ID
            domain: Device domain
            action: Action to perform
            value: Optional value for set actions

        Returns:
            CommandResponse with result
        """
        # Resolve action through domain handler (maps aliases to canonical names)
        from device_families.domains import get_domain_handler

        handler = get_domain_handler(domain)
        if handler:
            resolved = handler.resolve_action(action)
            if resolved:
                action = resolved.name

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

        # Try direct device control first
        direct_result = await self._try_direct_control(entity_id, action, data)
        if direct_result is not None:
            return direct_result

        # Fall back to Home Assistant
        try:
            service = HomeAssistantService()
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

    async def _try_direct_control(
        self,
        entity_id: str,
        action: str,
        data: Optional[Dict[str, Any]],
    ) -> Optional[CommandResponse]:
        """Attempt direct WiFi device control if entity is a direct device.

        Returns:
            CommandResponse if this is a direct device, None to fall through to HA.
        """
        try:
            from services.direct_device_service import DirectDeviceService
            direct_svc = _get_direct_device_service()
            if direct_svc is None or not direct_svc.is_direct_device(entity_id):
                return None

            result = await direct_svc.control_device(entity_id, action, data)

            if result.success:
                domain = get_domain_from_entity_id(entity_id) or "unknown"
                friendly_action = get_action_display_name(action)
                return CommandResponse.success_response(
                    context_data={
                        "entity_id": entity_id,
                        "domain": domain,
                        "action": action,
                        "source": "direct",
                        "message": f"Successfully executed {friendly_action} on {entity_id}",
                    },
                    wait_for_input=False,
                )
            else:
                return CommandResponse.error_response(
                    error_details=f"Failed to control device: {result.error}",
                    context_data={"error": result.error, "source": "direct"},
                )
        except ImportError:
            return None
        except Exception as e:
            logger.warning("Direct device control failed, will try HA", entity_id=entity_id, error=str(e))
            return None

    def handle_action(self, action_name: str, context: Dict[str, Any]) -> CommandResponse:
        """Handle a device control action from the mobile app.

        Called when the user taps an action button (e.g. Turn On, Turn Off)
        on the DeviceEditScreen. Routes to the appropriate device service.

        The CC control endpoint merges body.data into context, so action-
        specific values like temperature and mode live in context alongside
        entity_id, protocol, etc. We extract them as `data` for the adapter.
        """
        entity_id: str = context.get("entity_id", "")
        if not entity_id:
            return CommandResponse.error_response(
                error_details="Missing entity_id in action context.",
            )

        # Extract action-specific data from context (CC merges body.data into context)
        data: Optional[Dict[str, Any]] = _extract_action_data(action_name, context)

        # Run async control in a new event loop (MQTT listener thread has none)
        loop = asyncio.new_event_loop()
        try:
            result = loop.run_until_complete(
                self._execute_single_action(entity_id, action_name, data=data, context=context)
            )
        finally:
            loop.close()

        if result.get("success"):
            return CommandResponse.final_response(
                context_data={
                    "message": f"Done — {action_name.replace('_', ' ')} on {entity_id}.",
                },
            )
        return CommandResponse.error_response(
            error_details=result.get("error") or f"Failed to {action_name} on {entity_id}.",
        )

    async def _execute_single_action(
        self, entity_id: str, action: str, data: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Execute a single device action, routing to direct or HA service."""
        # Try the agent scheduler's DirectDeviceService first
        direct_svc = _get_direct_device_service()

        if direct_svc and direct_svc.is_direct_device(entity_id):
            result = await direct_svc.control_device(entity_id, action, data)
            return {"entity_id": entity_id, "success": result.success, "error": result.error}

        # Use protocol/device info from context to call the adapter directly.
        # This handles both cases: no agent scheduler, and device not in cache.
        try:
            result = await self._control_via_adapter(entity_id, action, data, context)
            if result is not None:
                return result
        except Exception as e:
            logger.warning("Direct adapter control failed", error=str(e))

        # Fall back to Home Assistant
        try:
            ha_service = HomeAssistantService()
            result = await ha_service.control_device(entity_id, action, data)
            return {"entity_id": entity_id, "success": result.success, "error": result.error}
        except Exception as e:
            return {"entity_id": entity_id, "success": False, "error": str(e)}


    async def _control_via_adapter(
        self, entity_id: str, action: str, data: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Control a device using protocol/device info from the action context.

        The CC control endpoint embeds protocol, cloud_id, model etc. in
        the MQTT context so the node can call the adapter directly without
        a round-trip back to CC. Action-specific values (temperature, mode,
        brightness) are also merged into context by the CC, so we extract
        them as data if not already provided.
        """
        from utils.device_family_discovery_service import get_device_family_discovery_service

        ctx = context or {}
        protocol: str = ctx.get("protocol") or ""
        if not protocol or ctx.get("source") != "direct":
            return None

        discovery = get_device_family_discovery_service()
        protocols = dict(discovery.get_all_families())
        adapter = protocols.get(protocol)
        if not adapter:
            return {"entity_id": entity_id, "success": False, "error": f"No adapter: {protocol}"}

        # CC merges body.data into context, so extract action-specific values
        # as adapter data if not already provided via the data parameter.
        effective_data = data
        if effective_data is None:
            effective_data = _extract_action_data(action, ctx)

        result = await adapter.control(
            ip=ctx.get("local_ip") or "",
            action=action,
            data=effective_data,
            entity_id=entity_id,
            mac_address=ctx.get("mac_address") or "",
            cloud_id=ctx.get("cloud_id") or "",
            model=ctx.get("model") or "",
        )
        return {"entity_id": entity_id, "success": result.success, "error": result.error}


def _extract_action_data(action: str, context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Extract action-specific data from a merged context dict.

    The CC control endpoint merges body.data into the MQTT context, so values
    like temperature and mode live alongside entity_id, protocol, etc.
    This extracts them into the dict shape that protocol adapters expect.
    """
    if action == "set_temperature" and "temperature" in context:
        return {"temperature": context["temperature"]}
    if action in ("set_mode", "set_hvac_mode") and "mode" in context:
        return {"mode": context["mode"]}
    if action == "set_brightness" and "brightness" in context:
        return {"brightness": context["brightness"]}
    if action == "set_percentage" and "percentage" in context:
        return {"percentage": context["percentage"]}
    if action == "set_hvac_mode" and "hvac_mode" in context:
        return {"mode": context["hvac_mode"]}
    if action == "set_color":
        if "rgb" in context:
            return {"rgb": context["rgb"]}
        if "color_temp" in context:
            return {"color_temp": context["color_temp"]}
        if "hue" in context and "saturation" in context:
            return {"hue": context["hue"], "saturation": context["saturation"]}
    return None


def _get_direct_device_service() -> Optional[Any]:
    """Get the DirectDeviceService singleton from agent scheduler context.

    Returns None if direct device control is not configured.
    """
    try:
        from services.agent_scheduler_service import get_agent_scheduler_service
        scheduler = get_agent_scheduler_service()
        return getattr(scheduler, "_direct_device_service", None)
    except Exception:
        return None
