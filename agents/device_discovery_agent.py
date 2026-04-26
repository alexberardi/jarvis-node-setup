"""Built-in device discovery agent for direct (non-HA) smart home devices.

Periodically fetches the household's device list from command center and
caches it locally. The cached data feeds into the system prompt via
get_context_data() so the LLM knows what devices are available, and the
control_device command can resolve entity_ids from memory without a
network hop.

Uses the agent name "home_assistant" so the existing prompt builders
(build_agent_context_summary, etc.) pick it up with zero changes. When
the HA Pantry package is installed, its custom agent overrides this one.
"""

from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import IJarvisSecret
from services.direct_device_service import DirectDeviceService
from utils.config_service import Config
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

# Refresh every 5 minutes — devices don't change often
_REFRESH_INTERVAL_SECONDS = 300


class DeviceDiscoveryAgent(IJarvisAgent):
    """Fetches direct devices from command center on a schedule."""

    def __init__(self) -> None:
        self._context: Dict[str, Any] = {}
        self._service: DirectDeviceService | None = None

    @property
    def name(self) -> str:
        # Same key the prompt builders look for in agents_data
        return "home_assistant"

    @property
    def description(self) -> str:
        return "Fetches smart home device list from command center for voice control"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=_REFRESH_INTERVAL_SECONDS,
            run_on_startup=True,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    def _get_service(self) -> DirectDeviceService:
        if self._service is None:
            self._service = DirectDeviceService(
                cc_base_url=get_command_center_url() or "",
                node_id=Config.get_str("node_id", "") or "",
                api_key=Config.get_str("api_key", "") or "",
                household_id=Config.get_str("household_id", "") or "",
            )
        return self._service

    async def run(self) -> None:
        try:
            service = self._get_service()
            count = await service.refresh_from_cc()
            devices = service.list_devices()

            # Build context in the same shape as the HA agent so
            # build_agent_context_summary works unchanged.
            device_controls: Dict[str, list] = {}
            for d in devices:
                domain = d.domain or "switch"
                device_controls.setdefault(domain, []).append({
                    "entity_id": d.entity_id,
                    "name": d.name,
                    "area": d.room_name or "",
                    "state": "unknown",
                })

            self._context = {"device_controls": device_controls}
            logger.info(
                "Device discovery refreshed",
                device_count=count,
                domains=list(device_controls.keys()),
            )

        except Exception as e:
            logger.error("Device discovery failed", error=str(e))
            self._context = {"last_error": str(e)}

    def get_context_data(self) -> Dict[str, Any]:
        return self._context
