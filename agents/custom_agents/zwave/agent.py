"""Z-Wave agent — background cache refresh for Z-Wave JS UI nodes.

Thin wrapper around ZWaveService. The agent interface lets the
AgentSchedulerService discover and run it on a timer.
"""

from typing import Any, Dict, List, Optional

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:  # noqa: E303
        def __init__(self, **kw: str) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: object) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: object) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: object) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: object) -> None:
            self._log.debug(msg)


from jarvis_command_sdk import AgentSchedule, IJarvisAgent, IJarvisSecret, JarvisSecret

logger = JarvisLogger(service="device.zwave")

# Refresh interval: 5 minutes
REFRESH_INTERVAL_SECONDS: int = 300


class ZWaveAgent(IJarvisAgent):
    """Agent that fetches Z-Wave node data from Z-Wave JS UI.

    Delegates all work to ZWaveService. The agent interface is
    preserved so the scheduler can discover and run it on a timer.
    """

    def __init__(self) -> None:
        self._service: Optional[Any] = None

    @property
    def name(self) -> str:
        return "zwave"

    @property
    def description(self) -> str:
        return "Fetches Z-Wave device data from Z-Wave JS UI for voice control"

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
                "ZWAVE_JS_URL",
                "Z-Wave JS Server WebSocket URL (e.g., ws://10.0.0.244:3000)",
                "integration", "string",
                required=False,
                is_sensitive=False,
                friendly_name="Z-Wave JS Server URL",
            ),
        ]

    def _get_service(self) -> Any:
        """Lazily create the ZWaveService instance."""
        if self._service is None:
            try:
                from device_families.custom_families.zwave.zwave_service import ZWaveService
            except ImportError:
                from device_families.zwave.zwave_service import ZWaveService

            self._service = ZWaveService()
        return self._service

    async def run(self) -> None:
        """Fetch node data from Z-Wave JS UI."""
        logger.info("ZWaveAgent.run() starting")
        try:
            service = self._get_service()
            await service.fetch_nodes()
            nodes = service.get_all_nodes()
            logger.info("ZWaveAgent.run() complete", cached_nodes=len(nodes), last_error=service._last_error)
        except Exception as e:
            logger.error("ZWaveAgent.run() failed", error=str(e), error_type=type(e).__name__)

    def get_context_data(self) -> Dict[str, Any]:
        """Return cached Z-Wave data for voice request context."""
        service = self._get_service()
        return service.get_context_data()
