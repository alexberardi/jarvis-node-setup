"""
Home Assistant agent for pre-fetching device and area data.

Connects to Home Assistant via WebSocket API to fetch:
- Areas (rooms)
- Devices
- Entities with current states

This data is cached and injected into voice request context, enabling
commands like "turn on the living room lights" to resolve device IDs.

WebSocket protocol reference:
https://developers.home-assistant.io/docs/api/websocket
"""

import asyncio
import json
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from jarvis_log_client import JarvisLogger

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from services.secret_service import get_secret_value

logger = JarvisLogger(service="jarvis-node")

# Refresh interval: 5 minutes
REFRESH_INTERVAL_SECONDS = 300

# WebSocket timeout for operations
WS_TIMEOUT_SECONDS = 30

# Common room names for area inference fallback
COMMON_ROOM_NAMES = [
    "living room", "bedroom", "kitchen", "bathroom", "office",
    "dining room", "garage", "basement", "attic", "hallway",
    "laundry", "closet", "patio", "porch", "deck", "yard",
    "master bedroom", "guest room", "kids room", "nursery",
    "family room", "den", "study", "library", "gym", "theater",
    "game room", "workshop", "utility room", "mud room", "foyer",
    "entrance", "front door", "back door", "side door",
]


class HomeAssistantAgent(IJarvisAgent):
    """Agent that fetches Home Assistant device and area data.

    Connects to HA WebSocket API, fetches registries, and caches the data
    for injection into voice request context.
    """

    # Maximum attempts to wait for a response with matching message ID
    MAX_RESPONSE_ATTEMPTS = 100

    def __init__(self) -> None:
        self._areas: List[Dict[str, Any]] = []
        self._devices: List[Dict[str, Any]] = []
        self._entities: List[Dict[str, Any]] = []
        self._states: Dict[str, Dict[str, Any]] = {}  # entity_id -> state
        self._last_refresh: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._message_id: int = 0
        self._message_id_lock: Optional[asyncio.Lock] = None

    @property
    def name(self) -> str:
        return "home_assistant"

    @property
    def description(self) -> str:
        return "Fetches Home Assistant device and area data for voice control"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=True,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "HOME_ASSISTANT_WS_URL",
                "Home Assistant WebSocket URL (e.g., ws://192.168.1.100:8123/api/websocket)",
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

    async def run(self) -> None:
        """Fetch device and area data from Home Assistant."""
        ws_url = get_secret_value("HOME_ASSISTANT_WS_URL", "integration")
        api_key = get_secret_value("HOME_ASSISTANT_API_KEY", "integration")

        if not ws_url or not api_key:
            self._last_error = "Missing HOME_ASSISTANT_WS_URL or HOME_ASSISTANT_API_KEY"
            logger.warning("Home Assistant agent skipped", reason=self._last_error)
            return

        try:
            await self._fetch_all_data(ws_url, api_key)
            self._last_refresh = datetime.now(timezone.utc)
            self._last_error = None
            logger.info(
                "Home Assistant data refreshed",
                areas=len(self._areas),
                devices=len(self._devices),
                entities=len(self._entities),
            )
        except asyncio.TimeoutError:
            self._last_error = "Connection timeout"
            logger.error("Home Assistant connection timeout")
        except ConnectionRefusedError:
            self._last_error = "Connection refused - is Home Assistant running?"
            logger.error("Home Assistant connection refused", url=ws_url)
        except Exception as e:
            self._last_error = str(e)
            logger.error("Home Assistant agent error", error=str(e))

    async def _fetch_all_data(self, ws_url: str, api_key: str) -> None:
        """Connect to HA WebSocket and fetch all registry data."""
        try:
            import websockets
        except ImportError:
            raise ImportError(
                "websockets package is required. Install with: pip install websockets"
            )

        async with websockets.connect(ws_url) as websocket:
            # Authenticate
            await self._authenticate(websocket, api_key)

            # Fetch registries sequentially (WebSocket can't handle concurrent recv)
            try:
                self._areas = await self._send_command(websocket, "config/area_registry/list") or []
            except Exception as e:
                logger.warning("Failed to fetch areas", error=str(e))
                self._areas = []

            try:
                self._devices = await self._send_command(websocket, "config/device_registry/list") or []
            except Exception as e:
                logger.warning("Failed to fetch devices", error=str(e))
                self._devices = []

            try:
                self._entities = await self._send_command(websocket, "config/entity_registry/list") or []
            except Exception as e:
                logger.warning("Failed to fetch entities", error=str(e))
                self._entities = []

            try:
                states_list = await self._send_command(websocket, "get_states") or []
                self._states = {s["entity_id"]: s for s in states_list}
            except Exception as e:
                logger.warning("Failed to fetch states", error=str(e))
                self._states = {}

    async def _authenticate(self, websocket: Any, api_key: str) -> None:
        """Authenticate with Home Assistant WebSocket API."""
        # Wait for auth_required message
        auth_required = await asyncio.wait_for(
            websocket.recv(),
            timeout=WS_TIMEOUT_SECONDS
        )
        auth_msg = json.loads(auth_required)

        if auth_msg.get("type") != "auth_required":
            raise ValueError(f"Unexpected message type: {auth_msg.get('type')}")

        # Send auth message
        await websocket.send(json.dumps({
            "type": "auth",
            "access_token": api_key
        }))

        # Wait for auth response
        auth_response = await asyncio.wait_for(
            websocket.recv(),
            timeout=WS_TIMEOUT_SECONDS
        )
        auth_result = json.loads(auth_response)

        if auth_result.get("type") == "auth_invalid":
            raise ValueError(f"Authentication failed: {auth_result.get('message')}")

        if auth_result.get("type") != "auth_ok":
            raise ValueError(f"Unexpected auth response: {auth_result.get('type')}")

        logger.debug("Home Assistant authenticated", ha_version=auth_result.get("ha_version"))

    async def _send_command(self, websocket: Any, command_type: str) -> Any:
        """Send a command and wait for the response.

        Args:
            websocket: WebSocket connection
            command_type: HA command type (e.g., 'config/area_registry/list')

        Returns:
            The 'result' field from the response

        Raises:
            ValueError: If command fails or response not received
        """
        # Thread-safe message ID increment for concurrent gather calls
        if self._message_id_lock is None:
            self._message_id_lock = asyncio.Lock()

        async with self._message_id_lock:
            self._message_id += 1
            msg_id = self._message_id

        await websocket.send(json.dumps({
            "id": msg_id,
            "type": command_type
        }))

        # Wait for response with matching ID (with max attempts to prevent infinite loop)
        attempts = 0
        while attempts < self.MAX_RESPONSE_ATTEMPTS:
            attempts += 1
            response = await asyncio.wait_for(
                websocket.recv(),
                timeout=WS_TIMEOUT_SECONDS
            )
            msg = json.loads(response)

            if msg.get("id") == msg_id:
                if not msg.get("success", True):
                    raise ValueError(f"Command failed: {msg.get('error', {}).get('message')}")
                return msg.get("result")

        raise ValueError(
            f"Did not receive response for message ID {msg_id} after {self.MAX_RESPONSE_ATTEMPTS} attempts"
        )

    def get_context_data(self) -> Dict[str, Any]:
        """Return cached Home Assistant data for voice request context."""
        # Build area ID to name mapping
        area_map = {area["area_id"]: area["name"] for area in self._areas}

        # Build device list with area names and entities
        devices_with_context = []
        for device in self._devices:
            area_id = device.get("area_id")
            area_name = area_map.get(area_id) if area_id else None

            # Find entities for this device
            device_entities = []
            inferred_area: Optional[str] = None  # Initialize before loop

            for entity in self._entities:
                if entity.get("device_id") == device.get("id"):
                    entity_id = entity.get("entity_id", "")
                    state_data = self._states.get(entity_id, {})

                    # Infer area from entity name if not set on device (only once)
                    if not area_name and not inferred_area:
                        inferred_area = self._infer_area_from_name(
                            entity.get("name") or entity.get("original_name") or entity_id
                        )

                    device_entities.append({
                        "entity_id": entity_id,
                        "name": entity.get("name") or entity.get("original_name"),
                        "platform": entity.get("platform"),
                        "state": state_data.get("state"),
                        "attributes": state_data.get("attributes", {}),
                    })

            devices_with_context.append({
                "id": device.get("id"),
                "name": device.get("name") or device.get("name_by_user"),
                "manufacturer": device.get("manufacturer"),
                "model": device.get("model"),
                "area": area_name or inferred_area,
                "entities": device_entities,
            })

        # Build light_controls mapping: room name -> entity info
        # This provides an LLM-friendly view for light control commands
        light_controls = self._build_light_controls()

        # Build device_controls mapping: all controllable devices by domain
        # This provides an LLM-friendly view for generic device control
        device_controls = self._build_device_controls()

        return {
            "devices": devices_with_context,
            "areas": [area["name"] for area in self._areas],
            "device_count": len(self._devices),
            "entity_count": len(self._entities),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "last_error": self._last_error,
            "light_controls": light_controls,
            "device_controls": device_controls,
        }

    def _build_light_controls(self) -> Dict[str, Dict[str, Any]]:
        """Build room→light mapping for LLM context.

        Identifies room light groups (Hue rooms with hue_type=room or is_hue_group)
        which provide single-entity control for all lights in a room.

        Returns:
            Dict mapping room names to entity info:
            {
                "Basement": {"entity_id": "light.basement", "state": "off", "type": "room_group"},
                "My office": {"entity_id": "light.my_office", "state": "on", "type": "room_group"}
            }
        """
        light_controls: Dict[str, Dict[str, Any]] = {}

        for entity_id, state_data in self._states.items():
            if not entity_id.startswith("light."):
                continue

            attrs = state_data.get("attributes", {})
            friendly_name = attrs.get("friendly_name", entity_id)

            # Check if this is a room group (Hue room groups have hue_type="room")
            is_room_group = (
                attrs.get("hue_type") == "room" or
                attrs.get("is_hue_group") is True
            )

            if is_room_group:
                light_controls[friendly_name] = {
                    "entity_id": entity_id,
                    "state": state_data.get("state"),  # "on" or "off"
                    "type": "room_group",
                }

        return light_controls

    def _build_device_controls(self) -> Dict[str, List[Dict[str, Any]]]:
        """Build domain→devices mapping for LLM context.

        Groups all controllable entities by domain for generic device control.
        Excludes sensor-only domains (sensor, binary_sensor, weather, etc.)

        Returns:
            Dict mapping domain to list of device info:
            {
                "light": [
                    {"entity_id": "light.basement", "name": "Basement", "state": "off"},
                    ...
                ],
                "cover": [
                    {"entity_id": "cover.garage_door", "name": "Garage Door", "state": "closed"},
                    ...
                ],
                ...
            }
        """
        # Domains that support control actions
        controllable_domains = {
            "light", "switch", "cover", "lock", "climate", "fan",
            "media_player", "vacuum", "script", "scene", "input_boolean",
            "automation", "humidifier", "water_heater",
        }

        device_controls: Dict[str, List[Dict[str, Any]]] = {}

        for entity_id, state_data in self._states.items():
            # Parse domain from entity_id
            if "." not in entity_id:
                continue

            domain = entity_id.split(".", 1)[0]

            if domain not in controllable_domains:
                continue

            attrs = state_data.get("attributes", {})
            friendly_name = attrs.get("friendly_name", entity_id)

            device_info = {
                "entity_id": entity_id,
                "name": friendly_name,
                "state": state_data.get("state"),
            }

            # Add domain-specific attributes that might be useful
            if domain == "climate":
                device_info["current_temperature"] = attrs.get("current_temperature")
                device_info["target_temperature"] = attrs.get("temperature")
                device_info["hvac_modes"] = attrs.get("hvac_modes", [])
            elif domain == "cover":
                device_info["current_position"] = attrs.get("current_position")
            elif domain == "fan":
                device_info["percentage"] = attrs.get("percentage")
            elif domain == "media_player":
                device_info["volume_level"] = attrs.get("volume_level")
                device_info["media_title"] = attrs.get("media_title")

            if domain not in device_controls:
                device_controls[domain] = []

            device_controls[domain].append(device_info)

        return device_controls

    def _infer_area_from_name(self, name: str) -> Optional[str]:
        """Try to infer area/room from entity or device name.

        Home Assistant sometimes doesn't populate area_id on devices.
        This fallback looks for common room names in the entity name.

        Args:
            name: Entity or device name

        Returns:
            Inferred area name or None
        """
        if not name:
            return None

        name_lower = name.lower()

        for room in COMMON_ROOM_NAMES:
            if room in name_lower:
                # Return proper case
                return room.title()

        return None
