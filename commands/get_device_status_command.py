"""
Get Device Status command for Jarvis.

Queries the current state of any Home Assistant device.
Returns state and relevant attributes for the LLM to describe.
"""

import asyncio
from typing import Any, Dict, List, Optional

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation
from services.home_assistant_service import (
    HomeAssistantService,
    get_domain_from_entity_id,
)
from utils.entity_resolver import resolve_entity_id


class GetDeviceStatusCommand(IJarvisCommand):
    """Command for querying Home Assistant device status.

    Queries state of any HA entity and returns relevant info
    for the LLM to describe to the user.
    """

    @property
    def command_name(self) -> str:
        return "get_device_status"

    @property
    def description(self) -> str:
        return (
            "Query the current status of a Home Assistant device. "
            "Returns state and attributes like temperature, position, etc. "
            "Use device_controls in context to find entity IDs."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "status",
            "state",
            "check",
            "is",
            "are",
            "what",
            "open",
            "closed",
            "locked",
            "unlocked",
            "on",
            "off",
            "temperature",
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
            "Check device_controls in context to find entity_id for the device",
            "Match user's device description to friendly_name in context",
            "For ambiguous requests, ask user to clarify which device",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use entity_id from device_controls context, not invented IDs",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration."""
        return [
            CommandExample(
                voice_command="Is the office light on?",
                expected_parameters={"entity_id": "light.my_office"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Are the basement lights on?",
                expected_parameters={"entity_id": "light.basement"},
            ),
            CommandExample(
                voice_command="Is the garage door open?",
                expected_parameters={"entity_id": "cover.garage_door"},
            ),
            CommandExample(
                voice_command="What's the thermostat set to?",
                expected_parameters={"entity_id": "climate.thermostat"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Dynamically generates examples from real HA entities when available,
        falling back to hardcoded static examples if HA is unreachable.
        """
        from utils.ha_training_data import generate_status_examples, get_ha_training_data

        ha_data = get_ha_training_data()
        if ha_data:
            dynamic = generate_status_examples(
                ha_data.get("device_controls", {}),
                ha_data.get("light_controls", {}),
            )
            if dynamic:
                return dynamic

        return self._static_adapter_examples()

    def _static_adapter_examples(self) -> List[CommandExample]:
        """Fallback static examples when HA is unreachable."""
        items = [
            # Light queries - office
            ("Is the office light on?", {"entity_id": "light.my_office"}),
            ("Are the office lights on?", {"entity_id": "light.my_office"}),
            ("Check if the office lights are on", {"entity_id": "light.my_office"}),
            ("Is the office light off?", {"entity_id": "light.my_office"}),
            # Light queries - office desk
            ("Is the desk light on?", {"entity_id": "light.office_desk"}),
            ("Check the office desk light", {"entity_id": "light.office_desk"}),
            # Light queries - office fan
            ("Is the office fan light on?", {"entity_id": "light.office_fan"}),
            # Light queries - basement
            ("Are the basement lights on?", {"entity_id": "light.basement"}),
            ("Is the basement light on?", {"entity_id": "light.basement"}),
            ("Check if the basement lights are on", {"entity_id": "light.basement"}),
            # Light queries - upstairs
            ("Are the upstairs lights on?", {"entity_id": "light.upstairs"}),
            ("What's the status of the upstairs lights?", {"entity_id": "light.upstairs"}),
            ("Is the upstairs light on?", {"entity_id": "light.upstairs"}),
            # Light queries - bathroom
            ("Is the bathroom light on?", {"entity_id": "light.middle_bathroom"}),
            ("Check if the bathroom light is on", {"entity_id": "light.middle_bathroom"}),
            ("Is the bathroom light off?", {"entity_id": "light.middle_bathroom"}),
            # Light queries - rest light
            ("Is the rest light on?", {"entity_id": "light.my_rest_light"}),
            ("Check the rest light", {"entity_id": "light.my_rest_light"}),
            # Switch queries - baby berardi timer
            ("Is the baby switch on?", {"entity_id": "switch.baby_berardi_timer"}),
            ("Is the baby timer on?", {"entity_id": "switch.baby_berardi_timer"}),
            ("Check the baby Berardi switch", {"entity_id": "switch.baby_berardi_timer"}),
            ("Is the baby Berardi timer on?", {"entity_id": "switch.baby_berardi_timer"}),
            # Cover/garage door queries
            ("Is the garage door open?", {"entity_id": "cover.garage_door"}),
            ("Is the garage open?", {"entity_id": "cover.garage_door"}),
            ("Check the garage door", {"entity_id": "cover.garage_door"}),
            ("Is the garage door closed?", {"entity_id": "cover.garage_door"}),
            # Lock queries
            ("Is the front door locked?", {"entity_id": "lock.front_door"}),
            ("Check if the front door is locked", {"entity_id": "lock.front_door"}),
            ("Is the back door unlocked?", {"entity_id": "lock.back_door"}),
            # Climate/thermostat queries
            ("What's the thermostat set to?", {"entity_id": "climate.thermostat"}),
            ("What temperature is it inside?", {"entity_id": "climate.thermostat"}),
            ("Check the thermostat", {"entity_id": "climate.thermostat"}),
            ("Is the AC on?", {"entity_id": "climate.thermostat"}),
            # Generic status
            ("Status of the bedroom fan", {"entity_id": "fan.bedroom"}),
            ("What's the vacuum doing?", {"entity_id": "vacuum.roborock"}),
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
        """Execute the get device status command.

        Args:
            request_info: Information about the request from JCC
            **kwargs: Parameters including 'entity_id'

        Returns:
            CommandResponse with device state and attributes
        """
        entity_id = kwargs.get("entity_id")

        if entity_id:
            entity_id = resolve_entity_id(entity_id, request_info.voice_command)

        if not entity_id:
            return CommandResponse.error_response(
                error_details="Entity ID is required. Which device do you want to check?",
                context_data={"error": "missing_entity_id"},
            )

        # Execute the query
        return asyncio.run(self._execute_query(entity_id))

    async def _execute_query(self, entity_id: str) -> CommandResponse:
        """Execute the state query via HomeAssistantService.

        Args:
            entity_id: HA entity ID to query

        Returns:
            CommandResponse with state data
        """
        try:
            service = HomeAssistantService()
            result = await service.get_state(entity_id)

            if result.success:
                domain = get_domain_from_entity_id(entity_id)

                # Build response data with relevant info
                context_data: Dict[str, Any] = {
                    "entity_id": entity_id,
                    "state": result.state,
                    "friendly_name": result.friendly_name or entity_id,
                    "domain": domain,
                }

                # Add domain-specific attributes
                if result.attributes:
                    context_data["attributes"] = self._filter_relevant_attributes(
                        domain, result.attributes
                    )

                return CommandResponse.success_response(
                    context_data=context_data,
                    wait_for_input=True,  # Allow follow-up questions
                )
            else:
                return CommandResponse.error_response(
                    error_details=f"Could not get status: {result.error}",
                    context_data={"error": result.error, "entity_id": entity_id},
                )

        except ValueError as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"error": "configuration_error"},
            )

    def _filter_relevant_attributes(
        self, domain: Optional[str], attributes: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Filter attributes to only include relevant ones for response.

        Args:
            domain: Device domain (e.g., "climate", "cover")
            attributes: Full attribute dict from HA

        Returns:
            Filtered dict with relevant attributes
        """
        # Common attributes to always exclude (technical/internal)
        exclude_keys = {
            "supported_features",
            "supported_color_modes",
            "color_mode",
            "icon",
            "entity_id",
            "attribution",
            "device_class",
            "state_class",
            "unit_of_measurement",
        }

        # Domain-specific relevant attributes
        relevant_by_domain: Dict[str, set] = {
            "climate": {
                "current_temperature",
                "temperature",
                "target_temp_high",
                "target_temp_low",
                "hvac_action",
                "hvac_modes",
                "fan_mode",
                "humidity",
            },
            "cover": {
                "current_position",
                "current_tilt_position",
            },
            "fan": {
                "percentage",
                "preset_mode",
                "preset_modes",
            },
            "media_player": {
                "volume_level",
                "is_volume_muted",
                "media_title",
                "media_artist",
                "media_album_name",
                "source",
                "source_list",
            },
            "lock": set(),  # State is enough
            "light": {
                "brightness",
                "color_temp",
                "rgb_color",
            },
            "vacuum": {
                "battery_level",
                "fan_speed",
            },
        }

        filtered: Dict[str, Any] = {}
        relevant_keys = relevant_by_domain.get(domain, set()) if domain else set()

        for key, value in attributes.items():
            if key in exclude_keys:
                continue

            # Include if in relevant list or if friendly_name
            if key == "friendly_name" or (relevant_keys and key in relevant_keys):
                filtered[key] = value
            # For unknown domains, include non-excluded attributes
            elif not relevant_keys and key not in exclude_keys:
                filtered[key] = value

        return filtered
