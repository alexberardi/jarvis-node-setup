"""
Home Assistant service for device control (REST) and data fetching (WebSocket).

Consolidates the former HomeAssistantAgent (data fetching, context building)
and HomeAssistantService (REST control) into a single service.

- Data fetching: WebSocket connection to fetch floors/areas/devices/entities/states
- Context building: Builds light_controls, device_controls, floors for voice context
- Device control: REST API calls for service actions (turn_on, turn_off, etc.)
"""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

import httpx
from jarvis_log_client import JarvisLogger

from services.secret_service import get_secret_value

logger = JarvisLogger(service="jarvis-node")


# Domain → allowed actions mapping
# Actions are HA service names (e.g., "turn_on" calls light.turn_on)
DOMAIN_ACTIONS: Dict[str, List[str]] = {
    "light": ["turn_on", "turn_off", "toggle"],
    "switch": ["turn_on", "turn_off", "toggle"],
    "cover": ["open_cover", "close_cover", "stop_cover", "toggle"],
    "lock": ["lock", "unlock"],
    "climate": ["set_temperature", "set_hvac_mode", "turn_on", "turn_off"],
    "fan": ["turn_on", "turn_off", "toggle", "set_percentage"],
    "media_player": ["turn_on", "turn_off", "toggle", "volume_up", "volume_down", "media_play", "media_pause", "media_stop"],
    "vacuum": ["start", "stop", "return_to_base", "locate"],
    "script": ["turn_on"],  # Scripts just run
    "scene": ["turn_on"],  # Scenes just activate
    "input_boolean": ["turn_on", "turn_off", "toggle"],
    "automation": ["turn_on", "turn_off", "toggle", "trigger"],
}

# Human-friendly action names for clarification prompts
ACTION_DISPLAY_NAMES: Dict[str, str] = {
    "turn_on": "turn on",
    "turn_off": "turn off",
    "toggle": "toggle",
    "open_cover": "open",
    "close_cover": "close",
    "stop_cover": "stop",
    "lock": "lock",
    "unlock": "unlock",
    "set_temperature": "set temperature",
    "set_hvac_mode": "set mode",
    "set_percentage": "set speed",
    "volume_up": "turn up volume",
    "volume_down": "turn down volume",
    "media_play": "play",
    "media_pause": "pause",
    "media_stop": "stop",
    "start": "start",
    "stop": "stop",
    "return_to_base": "return to base",
    "locate": "locate",
    "trigger": "trigger",
}

# Domains that support control actions
_CONTROLLABLE_DOMAINS = {
    "light", "switch", "cover", "lock", "climate", "fan",
    "media_player", "vacuum", "script", "scene", "input_boolean",
    "automation", "humidifier", "water_heater",
}

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

# Default cache staleness: 5 minutes
_DEFAULT_MAX_AGE_SECONDS = 300

# WebSocket timeout for operations
_WS_TIMEOUT_SECONDS = 30

# Max attempts to match a WebSocket response by message ID
_MAX_RESPONSE_ATTEMPTS = 100


class LightAction(Enum):
    """Valid light control actions."""

    TURN_ON = "turn_on"
    TURN_OFF = "turn_off"


@dataclass
class ServiceCallResult:
    """Result of a Home Assistant service call."""

    success: bool
    entity_id: str
    action: str
    error: Optional[str] = None


@dataclass
class EntityStateResult:
    """Result of a state query."""

    success: bool
    entity_id: str
    state: Optional[str] = None
    attributes: Optional[Dict[str, Any]] = None
    friendly_name: Optional[str] = None
    error: Optional[str] = None


def get_domain_from_entity_id(entity_id: str) -> Optional[str]:
    """Extract domain from entity_id.

    Args:
        entity_id: HA entity ID (e.g., "light.basement")

    Returns:
        Domain string (e.g., "light") or None if invalid format
    """
    if "." in entity_id:
        return entity_id.split(".", 1)[0]
    return None


def get_actions_for_domain(domain: str) -> List[str]:
    """Get allowed actions for a domain.

    Args:
        domain: HA domain (e.g., "light", "cover")

    Returns:
        List of allowed action names, empty if domain unknown
    """
    return DOMAIN_ACTIONS.get(domain, [])


def get_action_display_name(action: str) -> str:
    """Get human-friendly display name for an action.

    Args:
        action: HA service name (e.g., "open_cover")

    Returns:
        Display name (e.g., "open")
    """
    return ACTION_DISPLAY_NAMES.get(action, action.replace("_", " "))


def _flag_reauth(error: str) -> None:
    """Flag HA provider as needing re-authentication."""
    try:
        from services.command_auth_service import set_needs_auth
        set_needs_auth("home_assistant", error)
    except Exception as e:
        logger.warning("Could not flag re-auth", error=str(e))


class HomeAssistantService:
    """Unified Home Assistant service: data fetching (WebSocket) + device control (REST).

    Data fetching:
        - Connects to HA WebSocket API to fetch registries (floors, areas, devices, entities, states)
        - Builds context data (light_controls, device_controls, floors) for voice prompts
        - Call refresh_if_stale() before get_context_data() to keep cache fresh

    Device control:
        - call_service(), control_light(), control_device(), get_state()
        - Uses REST API for one-shot commands

    Usage:
        service = HomeAssistantService()
        await service.refresh_if_stale()
        context = service.get_context_data()
        result = await service.control_device("light.basement", "turn_on")
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        ws_url: Optional[str] = None,
    ) -> None:
        """Initialize the service.

        Args:
            base_url: HA REST API URL. Falls back to HOME_ASSISTANT_REST_URL secret.
            api_key: HA long-lived access token. Falls back to HOME_ASSISTANT_API_KEY secret.
            ws_url: HA WebSocket URL. Falls back to HOME_ASSISTANT_WS_URL secret.

        Raises:
            ValueError: If REST credentials are not available.
        """
        self._base_url = base_url or get_secret_value("HOME_ASSISTANT_REST_URL", "integration")
        self._api_key = api_key or get_secret_value("HOME_ASSISTANT_API_KEY", "integration")
        self._ws_url = ws_url or get_secret_value("HOME_ASSISTANT_WS_URL", "integration")

        if not self._base_url or not self._api_key:
            raise ValueError(
                "HOME_ASSISTANT_REST_URL and HOME_ASSISTANT_API_KEY required. "
                "Set via secrets or pass to constructor."
            )

        # Ensure base URL doesn't have trailing slash
        self._base_url = self._base_url.rstrip("/")

        # Data cache (populated by fetch_registries)
        self._floors: List[Dict[str, Any]] = []
        self._areas: List[Dict[str, Any]] = []
        self._devices: List[Dict[str, Any]] = []
        self._entities: List[Dict[str, Any]] = []
        self._states: Dict[str, Dict[str, Any]] = {}
        self._last_refresh: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._message_id: int = 0
        self._message_id_lock: Optional[asyncio.Lock] = None

    # ------------------------------------------------------------------
    # Data fetching (from former HomeAssistantAgent)
    # ------------------------------------------------------------------

    async def refresh_if_stale(self, max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS) -> None:
        """Re-fetch HA data if the cache is older than max_age_seconds.

        Safe to call on every request — only fetches when stale.

        Args:
            max_age_seconds: Maximum cache age before refresh (default 300s / 5min)
        """
        if self._last_refresh is not None:
            age = (datetime.now(timezone.utc) - self._last_refresh).total_seconds()
            if age < max_age_seconds:
                return

        await self.fetch_registries()

    async def fetch_registries(self) -> None:
        """Connect to HA WebSocket and fetch all registry data.

        Populates internal caches: _floors, _areas, _devices, _entities, _states.
        """
        if not self._ws_url or not self._api_key:
            self._last_error = "Missing HOME_ASSISTANT_WS_URL or HOME_ASSISTANT_API_KEY"
            logger.warning("HA data fetch skipped", reason=self._last_error)
            return

        try:
            import websockets
        except ImportError:
            self._last_error = "websockets package not installed"
            logger.error("websockets package required for HA data fetching")
            return

        try:
            async with websockets.connect(self._ws_url) as websocket:
                await self._authenticate(websocket, self._api_key)

                # Fetch registries sequentially (WS can't handle concurrent recv)
                try:
                    self._floors = await self._send_command(websocket, "config/floor_registry/list") or []
                except Exception as e:
                    logger.debug("Failed to fetch floors (may not be supported)", error=str(e))
                    self._floors = []

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

            self._last_refresh = datetime.now(timezone.utc)
            self._last_error = None
            logger.info(
                "HA data refreshed",
                floors=len(self._floors),
                areas=len(self._areas),
                devices=len(self._devices),
                entities=len(self._entities),
            )
        except asyncio.TimeoutError:
            self._last_error = "Connection timeout"
            logger.error("HA WebSocket connection timeout")
        except ConnectionRefusedError:
            self._last_error = "Connection refused - is Home Assistant running?"
            logger.error("HA WebSocket connection refused", url=self._ws_url)
        except Exception as e:
            self._last_error = str(e)
            logger.error("HA data fetch error", error=str(e))

    async def _authenticate(self, websocket: Any, api_key: str) -> None:
        """Authenticate with Home Assistant WebSocket API."""
        auth_required = await asyncio.wait_for(
            websocket.recv(), timeout=_WS_TIMEOUT_SECONDS
        )
        auth_msg = json.loads(auth_required)

        if auth_msg.get("type") != "auth_required":
            raise ValueError(f"Unexpected message type: {auth_msg.get('type')}")

        await websocket.send(json.dumps({
            "type": "auth",
            "access_token": api_key,
        }))

        auth_response = await asyncio.wait_for(
            websocket.recv(), timeout=_WS_TIMEOUT_SECONDS
        )
        auth_result = json.loads(auth_response)

        if auth_result.get("type") == "auth_invalid":
            raise ValueError(f"Authentication failed: {auth_result.get('message')}")
        if auth_result.get("type") != "auth_ok":
            raise ValueError(f"Unexpected auth response: {auth_result.get('type')}")

        logger.debug("HA WebSocket authenticated", ha_version=auth_result.get("ha_version"))

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
        if self._message_id_lock is None:
            self._message_id_lock = asyncio.Lock()

        async with self._message_id_lock:
            self._message_id += 1
            msg_id = self._message_id

        await websocket.send(json.dumps({
            "id": msg_id,
            "type": command_type,
        }))

        attempts = 0
        while attempts < _MAX_RESPONSE_ATTEMPTS:
            attempts += 1
            response = await asyncio.wait_for(
                websocket.recv(), timeout=_WS_TIMEOUT_SECONDS
            )
            msg = json.loads(response)

            if msg.get("id") == msg_id:
                if not msg.get("success", True):
                    raise ValueError(f"Command failed: {msg.get('error', {}).get('message')}")
                return msg.get("result")

        raise ValueError(
            f"Did not receive response for message ID {msg_id} after {_MAX_RESPONSE_ATTEMPTS} attempts"
        )

    # ------------------------------------------------------------------
    # Context building (from former HomeAssistantAgent)
    # ------------------------------------------------------------------

    def get_context_data(self) -> Dict[str, Any]:
        """Return cached Home Assistant data for voice request context.

        Returns:
            Dict with light_controls, device_controls, floors, areas, etc.
        """
        area_map = {area["area_id"]: area["name"] for area in self._areas}

        # Build device list with area names and entities
        devices_with_context: List[Dict[str, Any]] = []
        for device in self._devices:
            area_id = device.get("area_id")
            area_name = area_map.get(area_id) if area_id else None

            device_entities: List[Dict[str, Any]] = []
            inferred_area: Optional[str] = None

            for entity in self._entities:
                if entity.get("device_id") == device.get("id"):
                    entity_id = entity.get("entity_id", "")
                    state_data = self._states.get(entity_id, {})

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

        light_controls = self._build_light_controls()
        device_controls = self._build_device_controls()
        floors = self._build_floor_map(area_map)

        return {
            "devices": devices_with_context,
            "areas": [area["name"] for area in self._areas],
            "floors": floors,
            "device_count": len(self._devices),
            "entity_count": len(self._entities),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "last_error": self._last_error,
            "light_controls": light_controls,
            "device_controls": device_controls,
        }

    def _build_floor_map(self, area_map: Dict[str, str]) -> Dict[str, List[str]]:
        """Build floor name → area names mapping."""
        floor_map: Dict[str, List[str]] = {}
        if not self._floors:
            return floor_map

        floor_id_to_name = {
            f.get("floor_id", ""): f.get("name", "")
            for f in self._floors
            if f.get("floor_id") and f.get("name")
        }

        for area in self._areas:
            floor_id = area.get("floor_id")
            if not floor_id:
                continue
            floor_name = floor_id_to_name.get(floor_id)
            area_name = area_map.get(area.get("area_id", ""))
            if floor_name and area_name:
                if floor_name not in floor_map:
                    floor_map[floor_name] = []
                floor_map[floor_name].append(area_name)

        return floor_map

    def _build_light_controls(self) -> Dict[str, Dict[str, Any]]:
        """Build room→light mapping for LLM context.

        Identifies room light groups (Hue rooms with hue_type=room or is_hue_group)
        which provide single-entity control for all lights in a room.
        """
        light_controls: Dict[str, Dict[str, Any]] = {}

        for entity_id, state_data in self._states.items():
            if not entity_id.startswith("light."):
                continue

            attrs = state_data.get("attributes", {})
            friendly_name = attrs.get("friendly_name", entity_id)

            is_room_group = (
                attrs.get("hue_type") == "room"
                or attrs.get("is_hue_group") is True
            )

            if is_room_group:
                light_controls[friendly_name] = {
                    "entity_id": entity_id,
                    "state": state_data.get("state"),
                    "type": "room_group",
                }

        return light_controls

    def _build_device_controls(self) -> Dict[str, List[Dict[str, Any]]]:
        """Build domain→devices mapping for LLM context.

        Groups all controllable entities by domain.
        Excludes sensor-only domains (sensor, binary_sensor, weather, etc.)
        """
        entity_area_map = self._build_entity_area_map()
        device_controls: Dict[str, List[Dict[str, Any]]] = {}

        for entity_id, state_data in self._states.items():
            if "." not in entity_id:
                continue

            domain = entity_id.split(".", 1)[0]
            if domain not in _CONTROLLABLE_DOMAINS:
                continue

            attrs = state_data.get("attributes", {})
            friendly_name = attrs.get("friendly_name", entity_id)

            device_info: Dict[str, Any] = {
                "entity_id": entity_id,
                "name": friendly_name,
                "state": state_data.get("state"),
            }

            area = entity_area_map.get(entity_id)
            if area:
                device_info["area"] = area

            # Domain-specific attributes
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

    def _build_entity_area_map(self) -> Dict[str, str]:
        """Build entity_id → area name mapping from device and entity registries."""
        area_map = {area["area_id"]: area["name"] for area in self._areas}
        entity_area: Dict[str, str] = {}

        for device in self._devices:
            area_id = device.get("area_id")
            area_name = area_map.get(area_id) if area_id else None
            if not area_name:
                continue
            for entity in self._entities:
                if entity.get("device_id") == device.get("id"):
                    eid = entity.get("entity_id", "")
                    if eid:
                        entity_area[eid] = area_name

        return entity_area

    @staticmethod
    def _infer_area_from_name(name: str) -> Optional[str]:
        """Try to infer area/room from entity or device name.

        Fallback when HA doesn't populate area_id on devices.
        """
        if not name:
            return None

        name_lower = name.lower()
        for room in COMMON_ROOM_NAMES:
            if room in name_lower:
                return room.title()

        return None

    # ------------------------------------------------------------------
    # REST API — Device control
    # ------------------------------------------------------------------

    def _get_headers(self) -> Dict[str, str]:
        """Build request headers with authentication."""
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    async def call_service(
        self,
        domain: str,
        service: str,
        entity_id: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> ServiceCallResult:
        """Call a Home Assistant service.

        Args:
            domain: Service domain (e.g., "light", "switch", "cover")
            service: Service name (e.g., "turn_on", "turn_off", "toggle")
            entity_id: Target entity (e.g., "light.basement")
            data: Additional service data (e.g., {"brightness": 255})

        Returns:
            ServiceCallResult with success/failure info
        """
        url = f"{self._base_url}/api/services/{domain}/{service}"
        payload: Dict[str, Any] = {"entity_id": entity_id}
        if data:
            payload.update(data)

        try:
            async with httpx.AsyncClient() as client:
                response = await client.post(
                    url,
                    headers=self._get_headers(),
                    json=payload,
                    timeout=10.0,
                )

                if response.status_code == 200:
                    logger.info(
                        "HA service call succeeded",
                        domain=domain,
                        service=service,
                        entity_id=entity_id,
                    )
                    return ServiceCallResult(
                        success=True,
                        entity_id=entity_id,
                        action=f"{domain}.{service}",
                    )
                elif response.status_code == 401:
                    error = "401 Unauthorized"
                    logger.error("HA auth failed — flagging re-auth", entity_id=entity_id)
                    _flag_reauth(error)
                    return ServiceCallResult(
                        success=False,
                        entity_id=entity_id,
                        action=f"{domain}.{service}",
                        error=error,
                    )
                else:
                    error = f"HTTP {response.status_code}: {response.text}"
                    logger.error("HA service call failed", error=error)
                    return ServiceCallResult(
                        success=False,
                        entity_id=entity_id,
                        action=f"{domain}.{service}",
                        error=error,
                    )

        except httpx.TimeoutException:
            error = "Request timed out"
            logger.error("HA service call timeout", entity_id=entity_id)
            return ServiceCallResult(
                success=False,
                entity_id=entity_id,
                action=f"{domain}.{service}",
                error=error,
            )
        except httpx.ConnectError as e:
            error = f"Connection failed: {e}"
            logger.error("HA connection error", error=str(e))
            return ServiceCallResult(
                success=False,
                entity_id=entity_id,
                action=f"{domain}.{service}",
                error=error,
            )
        except Exception as e:
            error = str(e)
            logger.error("HA service call error", error=error)
            return ServiceCallResult(
                success=False,
                entity_id=entity_id,
                action=f"{domain}.{service}",
                error=error,
            )

    async def control_light(
        self,
        entity_id: str,
        action: LightAction,
    ) -> ServiceCallResult:
        """Control a light entity.

        Args:
            entity_id: Light entity (e.g., "light.basement")
            action: LightAction.TURN_ON or LightAction.TURN_OFF

        Returns:
            ServiceCallResult with success/failure info
        """
        return await self.call_service("light", action.value, entity_id)

    async def get_state(self, entity_id: str) -> EntityStateResult:
        """Get current state of an entity.

        Args:
            entity_id: HA entity ID (e.g., "light.basement", "cover.garage_door")

        Returns:
            EntityStateResult with state and attributes
        """
        url = f"{self._base_url}/api/states/{entity_id}"

        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    url,
                    headers=self._get_headers(),
                    timeout=10.0,
                )

                if response.status_code == 200:
                    data = response.json()
                    attrs = data.get("attributes", {})
                    logger.info(
                        "HA state query succeeded",
                        entity_id=entity_id,
                        state=data.get("state"),
                    )
                    return EntityStateResult(
                        success=True,
                        entity_id=entity_id,
                        state=data.get("state"),
                        attributes=attrs,
                        friendly_name=attrs.get("friendly_name"),
                    )
                elif response.status_code == 401:
                    error = "401 Unauthorized"
                    logger.error("HA auth failed — flagging re-auth", entity_id=entity_id)
                    _flag_reauth(error)
                    return EntityStateResult(
                        success=False,
                        entity_id=entity_id,
                        error=error,
                    )
                elif response.status_code == 404:
                    error = f"Entity '{entity_id}' not found"
                    logger.warning("HA entity not found", entity_id=entity_id)
                    return EntityStateResult(
                        success=False,
                        entity_id=entity_id,
                        error=error,
                    )
                else:
                    error = f"HTTP {response.status_code}: {response.text}"
                    logger.error("HA state query failed", error=error)
                    return EntityStateResult(
                        success=False,
                        entity_id=entity_id,
                        error=error,
                    )

        except httpx.TimeoutException:
            error = "Request timed out"
            logger.error("HA state query timeout", entity_id=entity_id)
            return EntityStateResult(
                success=False,
                entity_id=entity_id,
                error=error,
            )
        except httpx.ConnectError as e:
            error = f"Connection failed: {e}"
            logger.error("HA connection error", error=str(e))
            return EntityStateResult(
                success=False,
                entity_id=entity_id,
                error=error,
            )
        except Exception as e:
            error = str(e)
            logger.error("HA state query error", error=error)
            return EntityStateResult(
                success=False,
                entity_id=entity_id,
                error=error,
            )

    async def control_device(
        self,
        entity_id: str,
        action: str,
        data: Optional[Dict[str, Any]] = None,
    ) -> ServiceCallResult:
        """Control any device using domain-appropriate service.

        Parses domain from entity_id and calls the appropriate service.

        Args:
            entity_id: HA entity ID (e.g., "cover.garage_door")
            action: Service action (e.g., "open_cover", "turn_on")
            data: Additional service data (e.g., {"temperature": 72})

        Returns:
            ServiceCallResult with success/failure info
        """
        domain = get_domain_from_entity_id(entity_id)
        if not domain:
            return ServiceCallResult(
                success=False,
                entity_id=entity_id,
                action=action,
                error=f"Invalid entity_id format: {entity_id}",
            )

        # Validate action is allowed for this domain
        allowed_actions = get_actions_for_domain(domain)
        if allowed_actions and action not in allowed_actions:
            return ServiceCallResult(
                success=False,
                entity_id=entity_id,
                action=action,
                error=f"Action '{action}' not valid for {domain}. Allowed: {', '.join(allowed_actions)}",
            )

        return await self.call_service(domain, action, entity_id, data)
