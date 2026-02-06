"""
Home Assistant service for device control via REST API.

Uses HA REST API for service calls (simpler than WebSocket for commands).
WebSocket is used by HomeAssistantAgent for data fetching.
"""

import httpx
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

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


class HomeAssistantService:
    """Client for Home Assistant REST API service calls.

    Provides async methods for calling HA services like light.turn_on.
    Uses REST API instead of WebSocket for simplicity in one-shot commands.

    Usage:
        service = HomeAssistantService()
        result = await service.control_light("light.basement", LightAction.TURN_ON)
        if result.success:
            print(f"Turned on {result.entity_id}")
    """

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
    ) -> None:
        """Initialize the service.

        Args:
            base_url: HA REST API URL (e.g., "http://192.168.1.100:8123").
                     Falls back to HOME_ASSISTANT_REST_URL secret.
            api_key: HA long-lived access token.
                    Falls back to HOME_ASSISTANT_API_KEY secret.

        Raises:
            ValueError: If required credentials are not available.
        """
        self._base_url = base_url or get_secret_value("HOME_ASSISTANT_REST_URL", "integration")
        self._api_key = api_key or get_secret_value("HOME_ASSISTANT_API_KEY", "integration")

        if not self._base_url or not self._api_key:
            raise ValueError(
                "HOME_ASSISTANT_REST_URL and HOME_ASSISTANT_API_KEY required. "
                "Set via secrets or pass to constructor."
            )

        # Ensure base URL doesn't have trailing slash
        self._base_url = self._base_url.rstrip("/")

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

        Convenience method for light service calls.

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
