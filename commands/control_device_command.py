"""
Control Device command for Jarvis.

Generic device control for any Home Assistant domain.
Uses domain-based action validation with clarification flow.
"""

import asyncio
from typing import Any, Dict, List, Optional

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation
from services.home_assistant_service import (
    DOMAIN_ACTIONS,
    HomeAssistantService,
    get_action_display_name,
    get_actions_for_domain,
    get_domain_from_entity_id,
)


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
            "Control a Home Assistant device: open/close covers, lock/unlock doors, "
            "adjust thermostat, etc. If action is unclear, will ask for clarification."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "open",
            "close",
            "lock",
            "unlock",
            "start",
            "stop",
            "set",
            "adjust",
            "control",
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
                    "Home Assistant entity ID (e.g., 'cover.garage_door', "
                    "'lock.front_door', 'climate.thermostat'). "
                    "Find in device_controls context."
                ),
            ),
            JarvisParameter(
                "action",
                "string",
                required=False,
                description=(
                    "Action to perform. Domain-specific: "
                    "covers use open_cover/close_cover/stop_cover, "
                    "locks use lock/unlock, "
                    "climate uses set_temperature/set_hvac_mode/turn_on/turn_off. "
                    "If unsure, omit and system will ask."
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
            ),
            JarvisSecret(
                "HOME_ASSISTANT_API_KEY",
                "Home Assistant long-lived access token",
                "integration",
                "string",
            ),
        ]

    @property
    def rules(self) -> List[str]:
        return [
            "Check device_controls in context to find entity_id",
            "If user intent is clear (e.g., 'open the garage'), include the action",
            "If action is ambiguous, omit it and let system ask for clarification",
            "For temperature settings, include value parameter",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use entity_id from device_controls context, not invented IDs",
            "Match action to the device domain (covers use open_cover, locks use lock)",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration."""
        return [
            CommandExample(
                voice_command="Open the garage door",
                expected_parameters={
                    "entity_id": "cover.garage_door",
                    "action": "open_cover",
                },
                is_primary=True,
            ),
            CommandExample(
                voice_command="Lock the front door",
                expected_parameters={
                    "entity_id": "lock.front_door",
                    "action": "lock",
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
                voice_command="Close the blinds",
                expected_parameters={
                    "entity_id": "cover.blinds",
                    "action": "close_cover",
                },
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training."""
        items = [
            # Cover/garage door controls
            ("Open the garage door", {"entity_id": "cover.garage_door", "action": "open_cover"}),
            ("Open the garage", {"entity_id": "cover.garage_door", "action": "open_cover"}),
            ("Close the garage door", {"entity_id": "cover.garage_door", "action": "close_cover"}),
            ("Close the garage", {"entity_id": "cover.garage_door", "action": "close_cover"}),
            ("Stop the garage door", {"entity_id": "cover.garage_door", "action": "stop_cover"}),
            ("Open the blinds", {"entity_id": "cover.blinds", "action": "open_cover"}),
            ("Close the blinds", {"entity_id": "cover.blinds", "action": "close_cover"}),
            ("Lower the shades", {"entity_id": "cover.shades", "action": "close_cover"}),
            ("Raise the shades", {"entity_id": "cover.shades", "action": "open_cover"}),
            # Lock controls
            ("Lock the front door", {"entity_id": "lock.front_door", "action": "lock"}),
            ("Unlock the front door", {"entity_id": "lock.front_door", "action": "unlock"}),
            ("Lock the back door", {"entity_id": "lock.back_door", "action": "lock"}),
            ("Unlock the garage door", {"entity_id": "lock.garage_door", "action": "unlock"}),
            # Climate controls
            ("Set the thermostat to 72", {"entity_id": "climate.thermostat", "action": "set_temperature", "value": "72"}),
            ("Set temperature to 68", {"entity_id": "climate.thermostat", "action": "set_temperature", "value": "68"}),
            ("Turn on the AC", {"entity_id": "climate.thermostat", "action": "turn_on"}),
            ("Turn off the heat", {"entity_id": "climate.thermostat", "action": "turn_off"}),
            ("Set thermostat to cool", {"entity_id": "climate.thermostat", "action": "set_hvac_mode", "value": "cool"}),
            # Fan controls
            ("Turn on the bedroom fan", {"entity_id": "fan.bedroom", "action": "turn_on"}),
            ("Turn off the fan", {"entity_id": "fan.bedroom", "action": "turn_off"}),
            ("Set fan to 50 percent", {"entity_id": "fan.bedroom", "action": "set_percentage", "value": "50"}),
            # Vacuum controls
            ("Start the vacuum", {"entity_id": "vacuum.roborock", "action": "start"}),
            ("Stop the vacuum", {"entity_id": "vacuum.roborock", "action": "stop"}),
            ("Send vacuum home", {"entity_id": "vacuum.roborock", "action": "return_to_base"}),
            # Switch controls
            ("Turn on the coffee maker", {"entity_id": "switch.coffee_maker", "action": "turn_on"}),
            ("Turn off the porch light", {"entity_id": "switch.porch_light", "action": "turn_off"}),
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

        # If no action provided, either auto-select (single option) or ask
        if not action:
            if len(allowed_actions) == 1:
                # Single-action domains (scene, script): auto-select
                action = allowed_actions[0]
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

        # Execute the action
        return asyncio.run(self._execute_control(entity_id, domain, action, value))

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
