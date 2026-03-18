"""Home Assistant device manager — lists devices from a Home Assistant instance.

Wraps HomeAssistantService.get_context_data() and maps each HA entity
to a normalized DeviceManagerDevice.
"""

from jarvis_log_client import JarvisLogger

from core.ijarvis_authentication import AuthenticationConfig
from core.ijarvis_device_manager import DeviceManagerDevice, IJarvisDeviceManager
from core.ijarvis_secret import IJarvisSecret, JarvisSecret

logger = JarvisLogger(service="jarvis-node")


class HomeAssistantDeviceManager(IJarvisDeviceManager):
    """Lists devices from a Home Assistant instance via WebSocket API."""

    @property
    def name(self) -> str:
        return "home_assistant"

    @property
    def friendly_name(self) -> str:
        return "Home Assistant"

    @property
    def description(self) -> str:
        return "Devices managed by your Home Assistant instance"

    @property
    def can_edit_devices(self) -> bool:
        return False

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "HOME_ASSISTANT_REST_URL",
                "Home Assistant REST API URL (e.g., http://192.168.1.100:8123)",
                "integration",
                "string",
                is_sensitive=False,
                friendly_name="REST URL",
            ),
            JarvisSecret(
                "HOME_ASSISTANT_API_KEY",
                "Home Assistant long-lived access token",
                "integration",
                "string",
                friendly_name="API Key",
            ),
        ]

    @property
    def authentication(self) -> AuthenticationConfig:
        return AuthenticationConfig(
            type="oauth",
            provider="home_assistant",
            friendly_name="Home Assistant",
            client_id="http://jarvis-node-mobile",
            keys=["access_token"],
            authorize_path="/auth/authorize",
            exchange_path="/auth/token",
            discovery_port=8123,
            discovery_probe_path="/api/",
            send_redirect_uri_in_exchange=False,
        )

    async def collect_devices(self) -> list[DeviceManagerDevice]:
        """Fetch devices from HA via WebSocket and map to DeviceManagerDevice."""
        from services.home_assistant_service import HomeAssistantService

        service = HomeAssistantService()
        try:
            await service.connect_and_fetch()
        except Exception as e:
            logger.error("Failed to connect to Home Assistant", error=str(e))
            raise

        context = service.get_context_data()
        devices: list[DeviceManagerDevice] = []

        for ha_device in context.get("devices", []):
            area_name: str | None = ha_device.get("area")
            manufacturer: str | None = ha_device.get("manufacturer")
            model: str | None = ha_device.get("model")
            device_name: str = ha_device.get("name") or "Unknown"

            for entity in ha_device.get("entities", []):
                entity_id: str = entity.get("entity_id", "")
                if not entity_id:
                    continue

                # Extract domain from entity_id (e.g., "light.living_room" → "light")
                domain = entity_id.split(".")[0] if "." in entity_id else "unknown"

                devices.append(DeviceManagerDevice(
                    name=entity.get("name") or device_name,
                    domain=domain,
                    entity_id=entity_id,
                    is_controllable=True,
                    manufacturer=manufacturer,
                    model=model,
                    protocol="home_assistant",
                    source="home_assistant",
                    area=area_name,
                    state=entity.get("state"),
                ))

        logger.info("Home Assistant device list complete", device_count=len(devices))
        return devices
