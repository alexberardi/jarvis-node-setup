"""
Bluetooth command for Jarvis.

Voice interface for scanning, pairing, connecting, and disconnecting
Bluetooth devices (phones and speakers) on Pi Zero nodes.
"""

from typing import List

from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.platform_abstraction import get_bluetooth_provider
from core.request_information import RequestInformation
from services.bluetooth_service import BluetoothRole, BluetoothService

logger = JarvisLogger(service="jarvis-node")

# Map user-facing role names to BluetoothRole
_ROLE_MAP = {
    "speaker": BluetoothRole.SOURCE,
    "phone": BluetoothRole.SINK,
    "bridge": BluetoothRole.BRIDGE,
}


class BluetoothCommand(IJarvisCommand):
    """Command for managing Bluetooth devices on Jarvis nodes."""

    def __init__(self) -> None:
        self._service: BluetoothService | None = None

    def _get_service(self) -> BluetoothService:
        if self._service is None:
            self._service = BluetoothService(get_bluetooth_provider())
        return self._service

    @property
    def command_name(self) -> str:
        return "bluetooth"

    @property
    def description(self) -> str:
        return (
            "Manage Bluetooth connections: scan for nearby devices, "
            "pair phones or speakers, connect, disconnect, or check status."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "bluetooth", "pair", "connect", "disconnect",
            "speaker", "discoverable", "unpair", "forget",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "action", "string", required=True,
                description="The Bluetooth action to perform.",
                enum_values=["scan", "pair", "connect", "disconnect", "forget", "status"],
            ),
            JarvisParameter(
                "device_name", "string", required=False,
                description="Name or partial name of the Bluetooth device (e.g. 'JBL', 'iPhone').",
            ),
            JarvisParameter(
                "role", "string", required=False,
                description="How to use this device: 'speaker' (Pi sends audio to it), "
                            "'phone' (receive audio from it), or 'bridge' (phone → Pi → speaker).",
                enum_values=["speaker", "phone", "bridge"],
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def critical_rules(self) -> List[str]:
        return [
            "For 'pair my phone', use action='pair' role='phone'.",
            "For 'connect to the <speaker name>', use action='connect' device_name='<speaker name>' role='speaker'.",
            "If no role specified, default: phones → 'phone', speakers/audio → 'speaker'.",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Pair my phone",
                expected_parameters={"action": "pair", "role": "phone"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="What Bluetooth devices are nearby?",
                expected_parameters={"action": "scan"},
            ),
            CommandExample(
                voice_command="Connect to the JBL speaker",
                expected_parameters={"action": "connect", "device_name": "JBL", "role": "speaker"},
            ),
            CommandExample(
                voice_command="Disconnect my phone",
                expected_parameters={"action": "disconnect", "device_name": "phone"},
            ),
            CommandExample(
                voice_command="Bluetooth status",
                expected_parameters={"action": "status"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items = [
            ("Pair my phone", {"action": "pair", "role": "phone"}),
            ("Make this speaker discoverable", {"action": "pair", "role": "phone"}),
            ("Scan for Bluetooth devices", {"action": "scan"}),
            ("What Bluetooth devices are nearby?", {"action": "scan"}),
            ("Find Bluetooth speakers", {"action": "scan"}),
            ("Connect to the JBL speaker", {"action": "connect", "device_name": "JBL", "role": "speaker"}),
            ("Connect to my Bose headphones", {"action": "connect", "device_name": "Bose", "role": "speaker"}),
            ("Connect to the kitchen speaker", {"action": "connect", "device_name": "kitchen speaker", "role": "speaker"}),
            ("Disconnect my phone", {"action": "disconnect", "device_name": "phone"}),
            ("Disconnect the speaker", {"action": "disconnect", "device_name": "speaker"}),
            ("Forget the JBL speaker", {"action": "forget", "device_name": "JBL"}),
            ("Unpair my phone", {"action": "forget", "device_name": "phone"}),
            ("Bluetooth status", {"action": "status"}),
            ("What's connected via Bluetooth?", {"action": "status"}),
            ("Show Bluetooth connections", {"action": "status"}),
        ]
        return [
            CommandExample(
                voice_command=vc,
                expected_parameters=params,
                is_primary=(i == 0),
            )
            for i, (vc, params) in enumerate(items)
        ]

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute a Bluetooth action."""
        service = self._get_service()

        if not service.is_available():
            return CommandResponse.error_response(
                error_details="Bluetooth is not available on this device.",
            )

        action: str = kwargs.get("action", "status")
        device_name: str | None = kwargs.get("device_name")
        role_str: str | None = kwargs.get("role")
        role = _ROLE_MAP.get(role_str, BluetoothRole.SINK) if role_str else BluetoothRole.SINK

        if action == "scan":
            return self._handle_scan(service)
        elif action == "pair":
            return self._handle_pair(service, role)
        elif action == "connect":
            return self._handle_connect(service, device_name, role)
        elif action == "disconnect":
            return self._handle_disconnect(service, device_name)
        elif action == "forget":
            return self._handle_forget(service, device_name)
        elif action == "status":
            return self._handle_status(service)
        else:
            return CommandResponse.error_response(
                error_details=f"Unknown Bluetooth action: {action}",
            )

    def _handle_scan(self, service: BluetoothService) -> CommandResponse:
        devices = service.scan_for_devices(timeout=10.0)
        if not devices:
            return CommandResponse.follow_up_response(
                context_data={"message": "No Bluetooth devices found nearby.", "devices": []},
            )
        device_list = [
            {"name": d.name, "mac": d.mac_address, "type": d.device_type}
            for d in devices
        ]
        return CommandResponse.follow_up_response(
            context_data={
                "message": f"Found {len(devices)} Bluetooth device(s).",
                "devices": device_list,
            },
        )

    def _handle_pair(self, service: BluetoothService, role: BluetoothRole) -> CommandResponse:
        if role == BluetoothRole.SINK:
            # Phone → Pi: make Pi discoverable
            success = service.make_discoverable(timeout=120)
            if success:
                return CommandResponse.follow_up_response(
                    context_data={
                        "message": "This device is now discoverable for 2 minutes. "
                                   "Open Bluetooth settings on your phone and look for this node.",
                        "discoverable": True,
                        "timeout_seconds": 120,
                    },
                )
            return CommandResponse.error_response(error_details="Failed to make device discoverable.")
        else:
            # For speaker/bridge, scan first then ask user to pick
            return self._handle_scan(service)

    def _handle_connect(self, service: BluetoothService, device_name: str | None, role: BluetoothRole) -> CommandResponse:
        if not device_name:
            return CommandResponse.error_response(
                error_details="Please specify which device to connect to.",
            )

        # Try to find device among paired devices first
        paired = service.get_paired_devices()
        match = self._find_device_by_name(paired, device_name)

        if match:
            # Already paired, just connect
            if service.connect_device(match.mac_address):
                service.configure_audio_route(match.mac_address, role)
                service.save_device(match, role)
                return CommandResponse.follow_up_response(
                    context_data={
                        "message": f"Connected to {match.name}.",
                        "device": {"name": match.name, "mac": match.mac_address},
                        "role": role.value,
                    },
                )
            return CommandResponse.error_response(
                error_details=f"Failed to connect to {match.name}.",
            )

        # Not paired yet — scan and try to find it
        devices = service.scan_for_devices(timeout=10.0)
        match = self._find_device_by_name(devices, device_name)

        if not match:
            return CommandResponse.error_response(
                error_details=f"Could not find a device matching '{device_name}'. "
                              "Make sure it's in pairing mode and try again.",
            )

        result = service.pair_and_connect(match.mac_address, role)
        if result.success:
            return CommandResponse.follow_up_response(
                context_data={
                    "message": result.message,
                    "device": {"name": match.name, "mac": match.mac_address},
                    "role": role.value,
                },
            )
        return CommandResponse.error_response(error_details=result.message)

    def _handle_disconnect(self, service: BluetoothService, device_name: str | None) -> CommandResponse:
        if not device_name:
            # Disconnect all
            connected = service.get_connected_devices()
            for d in connected:
                service.disconnect_device(d.mac_address)
            return CommandResponse.follow_up_response(
                context_data={"message": f"Disconnected {len(connected)} device(s)."},
            )

        connected = service.get_connected_devices()
        match = self._find_device_by_name(connected, device_name)
        if not match:
            return CommandResponse.error_response(
                error_details=f"No connected device matching '{device_name}'.",
            )

        service.disconnect_device(match.mac_address)
        return CommandResponse.follow_up_response(
            context_data={"message": f"Disconnected {match.name}."},
        )

    def _handle_forget(self, service: BluetoothService, device_name: str | None) -> CommandResponse:
        if not device_name:
            return CommandResponse.error_response(
                error_details="Please specify which device to forget.",
            )

        paired = service.get_paired_devices()
        match = self._find_device_by_name(paired, device_name)
        if not match:
            return CommandResponse.error_response(
                error_details=f"No paired device matching '{device_name}'.",
            )

        service.forget_device(match.mac_address)
        return CommandResponse.follow_up_response(
            context_data={"message": f"Forgot {match.name}. It will need to be paired again."},
        )

    def _handle_status(self, service: BluetoothService) -> CommandResponse:
        status = service.get_status()
        connected_names = [d["name"] for d in status["connected"]]
        paired_names = [d["name"] for d in status["paired"] if not d["connected"]]

        if not connected_names and not paired_names:
            message = "No Bluetooth devices paired or connected."
        else:
            parts = []
            if connected_names:
                parts.append(f"Connected: {', '.join(connected_names)}")
            if paired_names:
                parts.append(f"Paired (not connected): {', '.join(paired_names)}")
            message = ". ".join(parts) + "."

        return CommandResponse.follow_up_response(
            context_data={"message": message, **status},
        )

    @staticmethod
    def _find_device_by_name(devices: list, name: str):
        """Find a device by exact or partial name match (case-insensitive)."""
        name_lower = name.lower()
        # Exact match first
        for d in devices:
            if d.name.lower() == name_lower:
                return d
        # Partial match
        for d in devices:
            if name_lower in d.name.lower():
                return d
        return None
