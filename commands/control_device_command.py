"""Built-in control_device command for Jarvis.

Controls smart home devices via DirectDeviceService (WiFi protocols like
LIFX, Kasa, Tuya, etc.). Disabled automatically when the user enables an
external device manager (e.g., Home Assistant Pantry package) to prevent
duplicate control_device commands.
"""

import asyncio
from typing import Any, Dict, List

from core.command_response import CommandResponse
from jarvis_command_sdk import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation


# Persistent event loop for async device protocol calls.
_device_loop: asyncio.AbstractEventLoop | None = None


def _get_device_loop() -> asyncio.AbstractEventLoop:
    global _device_loop
    if _device_loop is None or _device_loop.is_closed():
        _device_loop = asyncio.new_event_loop()
    return _device_loop


class ControlDeviceCommand(IJarvisCommand):
    """Control smart home devices (turn on/off, play, pause, volume, etc.)."""

    @property
    def command_name(self) -> str:
        return "control_device"

    @property
    def description(self) -> str:
        return (
            "Control a smart home device: turn on/off, play, pause, volume, lock, unlock. "
            "MUST be used for any request to turn on, turn off, play, pause, or control "
            "a physical device (TV, light, speaker, lock, thermostat). "
            "Common actions: turn_on, turn_off, play, pause, volume_up, volume_down, "
            "next, previous, lock, unlock."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "turn on", "turn off", "power on", "power off",
            "play", "pause", "stop", "volume", "next", "previous",
            "lock", "unlock", "device", "tv", "light", "speaker",
            "switch", "thermostat", "dim", "brighten",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "device_name",
                "string",
                required=True,
                description="Name of the device to control (e.g. 'Apple TV', 'living room light').",
            ),
            JarvisParameter(
                "action",
                "string",
                required=True,
                description=(
                    "Action to perform: turn_on, turn_off, play, pause, "
                    "volume_up, volume_down, next, previous, lock, unlock, "
                    "set_temperature, set_mode, set_brightness."
                ),
            ),
            JarvisParameter(
                "entity_id",
                "string",
                required=False,
                description="Entity ID from the device list (e.g. 'light.office'). Pass if shown in context.",
            ),
            JarvisParameter(
                "value",
                "string",
                required=False,
                description=(
                    "Value for the action. Examples: temperature in degrees for set_temperature (e.g. '69'), "
                    "mode name for set_mode (e.g. 'heat', 'cool', 'off'), "
                    "brightness percentage for set_brightness (e.g. '50')."
                ),
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def rules(self) -> List[str]:
        return [
            "Always use control_device for physical device requests, never a routine.",
            "Match the device name to what the user said as closely as possible.",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "device_name and action are both required.",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Turn on the Apple TV",
                expected_parameters={"device_name": "Apple TV", "action": "turn_on"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Turn off the office light",
                expected_parameters={"device_name": "office light", "action": "turn_off"},
            ),
            CommandExample(
                voice_command="Set the thermostat to 72 degrees",
                expected_parameters={"device_name": "thermostat", "action": "set_temperature", "value": "72"},
            ),
            CommandExample(
                voice_command="Set the Nest to heat mode",
                expected_parameters={"device_name": "Nest Thermostat", "action": "set_mode", "value": "heat"},
            ),
            CommandExample(
                voice_command="Turn up the volume on the speaker",
                expected_parameters={"device_name": "speaker", "action": "volume_up"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items = [
            ("Turn on the Apple TV", {"device_name": "Apple TV", "action": "turn_on"}),
            ("Turn off the office light", {"device_name": "office light", "action": "turn_off"}),
            ("Pause the TV", {"device_name": "TV", "action": "pause"}),
            ("Turn up the volume on the speaker", {"device_name": "speaker", "action": "volume_up"}),
            ("Set the thermostat to 69 degrees", {"device_name": "thermostat", "action": "set_temperature", "value": "69"}),
            ("Set the Nest to cool mode", {"device_name": "Nest Thermostat", "action": "set_mode", "value": "cool"}),
            ("Turn on the living room lights", {"device_name": "living room lights", "action": "turn_on"}),
            ("Turn off the bedroom lamp", {"device_name": "bedroom lamp", "action": "turn_off"}),
            ("Lock the front door", {"device_name": "front door", "action": "lock"}),
            ("Unlock the back door", {"device_name": "back door", "action": "unlock"}),
            ("Turn off all the lights", {"device_name": "all lights", "action": "turn_off"}),
        ]
        examples = []
        for i, (utterance, params) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters=params,
                is_primary=(i == 0),
            ))
        return examples

    def handle_action(self, action_name: str, context: Dict[str, Any]) -> CommandResponse:
        """Handle device control actions dispatched via MQTT from CC.

        CC sends control_device actions with action_name (e.g., "turn_on")
        and a context dict containing entity_id, protocol, cloud_id, etc.
        """
        if action_name == "cancel_click":
            return CommandResponse.final_response(
                context_data={"cancelled": True, "message": "Cancelled."}
            )

        result = self._control_with_context(context, action_name)

        if not result.get("success"):
            return CommandResponse.error_response(
                error_details=result.get("error", "Control failed"),
                context_data=result,
            )

        ctx_data: Dict[str, Any] = {
            "device": result.get("device", ""),
            "action": action_name,
            "message": f"{action_name.replace('_', ' ').title()} {result.get('device', '')}",
        }
        # Pass through input_required (e.g., PIN entry for Apple TV pairing)
        if result.get("input_required"):
            ctx_data["input_required"] = result["input_required"]

        return CommandResponse.success_response(
            context_data=ctx_data,
            wait_for_input=False,
        )

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        device_name: str = kwargs.get("device_name", "")
        action: str = kwargs.get("action", "")

        if not device_name or not action:
            return CommandResponse.error_response(
                error_details="Both device_name and action are required.",
                context_data={"error": "missing_params"},
            )

        entity_id: str = kwargs.get("entity_id", "")
        value: str = kwargs.get("value", "")

        # Build action data from value param based on action type
        data: Dict[str, Any] = {}
        if value:
            if action == "set_temperature":
                data["temperature"] = value
            elif action == "set_mode":
                data["mode"] = value
            elif action == "set_brightness":
                data["brightness"] = value
            else:
                data["value"] = value

        try:
            if entity_id:
                # LLM passed entity_id from prompt context — dispatch directly
                result = self._control_by_entity_id(entity_id, action, device_name, data)
            else:
                result = self._control(device_name, action, data)
        except Exception as e:
            return CommandResponse.error_response(
                error_details=f"Device control failed: {e}",
                context_data={"error": str(e), "device_name": device_name, "action": action},
            )

        if not result.get("success"):
            error_msg: str = result.get("error", "Unknown error")
            ctx: Dict[str, Any] = {"error": error_msg, "device_name": device_name, "action": action}
            if "available_devices" in result:
                ctx["available_devices"] = result["available_devices"]
            return CommandResponse.error_response(error_details=error_msg, context_data=ctx)

        return CommandResponse.success_response(
            context_data={
                "device": result.get("device", device_name),
                "action": action,
                "message": f"{action.replace('_', ' ').title()} {result.get('device', device_name)}",
            },
            wait_for_input=False,
        )

    def _control_by_entity_id(
        self, entity_id: str, action: str, device_name: str, data: Dict[str, Any] | None = None,
    ) -> Dict[str, Any]:
        """Dispatch control using entity_id from the LLM.

        Resolves device details from the DirectDeviceService cache
        (populated by DeviceDiscoveryAgent). No network hop needed.
        """
        service = self._get_device_service()
        device = service.get_device(entity_id)

        if not device:
            # Cache might be cold — try refreshing once
            loop = _get_device_loop()
            loop.run_until_complete(service.refresh_from_cc())
            device = service.get_device(entity_id)

        if not device:
            return {"success": False, "error": f"Device '{entity_id}' not found"}

        loop = _get_device_loop()
        result = loop.run_until_complete(service.control_device(entity_id, action, data))

        if result.success:
            return {"success": True, "device": device.name, "action": action}
        return {"success": False, "error": result.error or "Control failed", "device": device.name}

    def _control_with_context(self, ctx: Dict[str, Any], action: str) -> Dict[str, Any]:
        """Dispatch control using pre-resolved device context."""
        from device_families.base import DeviceControlResult, DiscoveredDevice

        protocol_name: str = ctx.get("protocol", "")
        entity_id: str = ctx.get("entity_id", "")
        device_name: str = ctx.get("name", entity_id)

        if not protocol_name:
            return {"success": False, "error": f"No protocol for device '{device_name}'"}

        from utils.device_family_discovery_service import get_device_family_discovery_service

        svc = get_device_family_discovery_service()
        families = svc.get_all_families()
        adapter = families.get(protocol_name)

        if not adapter:
            all_families = svc.get_all_families_for_snapshot()
            adapter = all_families.get(protocol_name)

        if not adapter:
            return {"success": False, "error": f"Protocol '{protocol_name}' not available on this node"}

        # Build DiscoveredDevice — same pattern as _handle_device_protocol_control
        device = DiscoveredDevice(
            entity_id=entity_id,
            name=device_name,
            domain=ctx.get("domain", "switch"),
            manufacturer=protocol_name,
            model=ctx.get("model", ""),
            protocol=protocol_name,
            cloud_id=ctx.get("cloud_id"),
            local_ip=ctx.get("local_ip"),
            mac_address=ctx.get("mac_address"),
        )

        loop = _get_device_loop()
        result: DeviceControlResult = loop.run_until_complete(
            adapter.control(device, action, ctx)
        )

        out: Dict[str, Any] = {"success": result.success, "device": device_name, "action": action}
        if not result.success:
            out["error"] = result.error or "Control failed"
        if result.input_required:
            out["input_required"] = result.input_required.to_dict()
        return out

    def _control(self, device_name: str, action: str, data: Dict[str, Any] | None = None) -> Dict[str, Any]:
        """Resolve device and dispatch control via DirectDeviceService (fallback)."""
        from services.direct_device_service import DirectDeviceService

        service = self._get_device_service()
        devices = service.list_devices()

        if not devices:
            # Try refreshing from CC
            loop = _get_device_loop()
            loop.run_until_complete(service.refresh_from_cc())
            devices = service.list_devices()

        if not devices:
            return {"success": False, "error": "No devices available. Add devices first."}

        # Fuzzy match device by name
        match = self._match_device(devices, device_name)
        if not match:
            available = [d.name for d in devices]
            return {
                "success": False,
                "error": f"Device '{device_name}' not found.",
                "available_devices": available,
            }

        # Execute control
        loop = _get_device_loop()
        result = loop.run_until_complete(service.control_device(match.entity_id, action, data))

        if result.success:
            return {"success": True, "device": match.name, "action": action}
        return {"success": False, "error": result.error or "Control failed", "device": match.name}

    @staticmethod
    def _match_device(devices: list, query: str) -> Any:
        """Match a device by name (exact, then case-insensitive, then substring)."""
        query_lower = query.lower().strip()

        # Exact case-insensitive match
        for d in devices:
            if d.name.lower() == query_lower:
                return d

        # Substring match
        for d in devices:
            if query_lower in d.name.lower() or d.name.lower() in query_lower:
                return d

        # Entity ID match
        for d in devices:
            if query_lower in d.entity_id.lower():
                return d

        return None

    @staticmethod
    def _get_device_service() -> "DirectDeviceService":
        """Get or create the DirectDeviceService singleton."""
        from services.direct_device_service import DirectDeviceService
        from utils.config_service import Config
        from utils.service_discovery import get_command_center_url

        # Check if there's already a service instance on the module
        import commands.control_device_command as _self_mod
        if hasattr(_self_mod, "_device_service") and _self_mod._device_service is not None:
            return _self_mod._device_service

        cc_url: str = get_command_center_url() or ""
        node_id: str = Config.get_str("node_id", "") or ""
        api_key: str = Config.get_str("api_key", "") or ""
        household_id: str = Config.get_str("household_id", "") or ""

        service = DirectDeviceService(
            cc_base_url=cc_url,
            node_id=node_id,
            api_key=api_key,
            household_id=household_id,
        )
        _self_mod._device_service = service  # type: ignore[attr-defined]
        return service
