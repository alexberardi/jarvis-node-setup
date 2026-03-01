"""
Home Assistant agent — thin wrapper around HomeAssistantService.

All data fetching, context building, and WebSocket logic has been
consolidated into HomeAssistantService.  This agent remains so the
AgentSchedulerService can discover and schedule it automatically.
"""

from typing import Any, Dict, List, Optional

from jarvis_log_client import JarvisLogger

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import IJarvisSecret, JarvisSecret

logger = JarvisLogger(service="jarvis-node")

# Refresh interval: 5 minutes
REFRESH_INTERVAL_SECONDS = 300

# Re-export for test compatibility
COMMON_ROOM_NAMES = None  # Moved to services.home_assistant_service


class HomeAssistantAgent(IJarvisAgent):
    """Agent that fetches Home Assistant device and area data.

    Delegates all work to HomeAssistantService.  The agent interface
    is preserved so the scheduler can discover and run it on a timer.
    """

    def __init__(self) -> None:
        self._service: Optional[Any] = None  # Lazy init to avoid import-time secret checks

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

    def _get_service(self) -> Any:
        """Lazily create the HomeAssistantService instance."""
        if self._service is None:
            from services.home_assistant_service import HomeAssistantService
            try:
                self._service = HomeAssistantService()
            except ValueError as e:
                logger.warning("Could not create HomeAssistantService", error=str(e))
                return None
        return self._service

    async def run(self) -> None:
        """Fetch device and area data from Home Assistant."""
        service = self._get_service()
        if service is None:
            return
        await service.fetch_registries()

    def get_context_data(self) -> Dict[str, Any]:
        """Return cached Home Assistant data for voice request context."""
        service = self._get_service()
        if service is None:
            return {
                "last_error": "HomeAssistantService not initialized",
                "light_controls": {},
                "device_controls": {},
                "floors": {},
            }
        return service.get_context_data()
