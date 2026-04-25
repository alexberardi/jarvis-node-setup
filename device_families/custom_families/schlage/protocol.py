"""Schlage Encode WiFi smart lock protocol adapter (cloud)."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from jarvis_command_sdk import (
    DeviceControlResult,
    DiscoveredDevice,
    IJarvisButton,
    IJarvisDeviceProtocol,
    IJarvisSecret,
    JarvisSecret,
    JarvisStorage,
)

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:  # type: ignore[no-redef]
        def __init__(self, **kw: Any) -> None:
            self._log = logging.getLogger(kw.get("service", __name__))

        def info(self, msg: str, **kw: Any) -> None:
            self._log.info(msg)

        def warning(self, msg: str, **kw: Any) -> None:
            self._log.warning(msg)

        def error(self, msg: str, **kw: Any) -> None:
            self._log.error(msg)

        def debug(self, msg: str, **kw: Any) -> None:
            self._log.debug(msg)


logger = JarvisLogger(service="device.schlage")

_storage = JarvisStorage("schlage")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _get_credentials() -> tuple[str | None, str | None]:
    """Read Schlage account credentials from node secrets."""
    username = _storage.get_secret("SCHLAGE_USERNAME", scope="integration")
    password = _storage.get_secret("SCHLAGE_PASSWORD", scope="integration")
    return username, password


def _build_schlage_client():
    """Authenticate and return a SchlageClient instance.

    Runs the Cognito SRP handshake synchronously — callers should
    wrap in asyncio.to_thread().
    """
    from .schlage_client import CognitoSRPAuth, SchlageClient

    username, password = _get_credentials()
    if not username or not password:
        raise ValueError("SCHLAGE_USERNAME and SCHLAGE_PASSWORD must be configured")

    auth = CognitoSRPAuth(username, password)
    auth.authenticate()
    return SchlageClient(auth)


class SchlageProtocol(IJarvisDeviceProtocol):
    """Schlage Encode WiFi smart lock protocol adapter."""

    protocol_name: str = "schlage"
    friendly_name: str = "Schlage"
    supported_domains: list[str] = ["lock"]
    connection_type: str = "cloud"
    setup_guide: str = """## Schlage Account Credentials

This integration uses your Schlage Home app account to control Encode
WiFi locks via the Allegion cloud API.

1. Enter the **email** and **password** you use to log into the
   Schlage Home app
2. The integration authenticates via AWS Cognito (same as the app)
3. All locks on your account will be discoverable

**Note:** Schlage does not offer a public API — this uses the same
backend as the mobile app. Your credentials are stored encrypted on
the node and never leave it."""

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
        return [
            JarvisSecret(
                "SCHLAGE_USERNAME",
                "Schlage Home app email address",
                "integration",
                "string",
                is_sensitive=False,
                required=True,
                friendly_name="Email",
            ),
            JarvisSecret(
                "SCHLAGE_PASSWORD",
                "Schlage Home app password",
                "integration",
                "string",
                is_sensitive=True,
                required=True,
                friendly_name="Password",
            ),
        ]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(
                button_text="Lock",
                button_action="lock",
                button_type="primary",
                button_icon="lock",
            ),
            IJarvisButton(
                button_text="Unlock",
                button_action="unlock",
                button_type="secondary",
                button_icon="lock-open",
            ),
        ]

    async def discover(self, timeout: int = 10) -> list[DiscoveredDevice]:
        username, password = _get_credentials()
        if not username or not password:
            logger.error("Schlage credentials not configured")
            return []

        try:
            client = await asyncio.to_thread(_build_schlage_client)
            locks = await asyncio.to_thread(client.get_locks)
        except Exception as e:
            logger.error(f"Schlage discovery failed: {e}")
            return []

        devices: list[DiscoveredDevice] = []
        for lock in locks:
            entity_id = _slugify(lock.name) if lock.name else _slugify(lock.device_id)
            devices.append(
                DiscoveredDevice(
                    entity_id=entity_id,
                    name=lock.name or "Schlage Lock",
                    domain="lock",
                    protocol=self.protocol_name,
                    model=lock.model_name or "",
                    manufacturer="Schlage",
                    cloud_id=lock.device_id,
                    extra={
                        "is_locked": lock.is_locked,
                        "is_jammed": lock.is_jammed,
                        "battery_level": lock.battery_level,
                        "connected": lock.connected,
                    },
                )
            )

        logger.info(f"Schlage discovery found {len(devices)} lock(s)")
        return devices

    async def control(
        self,
        device: DiscoveredDevice,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> DeviceControlResult:
        username, password = _get_credentials()
        if not username or not password:
            return DeviceControlResult(
                success=False,
                entity_id=device.entity_id,
                action=action,
                error="Schlage credentials not configured",
            )

        try:
            client = await asyncio.to_thread(_build_schlage_client)
        except Exception as e:
            return DeviceControlResult(
                success=False,
                entity_id=device.entity_id,
                action=action,
                error=f"Schlage auth failed: {e}",
            )

        # Find the target lock by cloud_id or entity_id
        try:
            locks = await asyncio.to_thread(client.get_locks)
        except Exception as e:
            return DeviceControlResult(
                success=False,
                entity_id=device.entity_id,
                action=action,
                error=f"Failed to list locks: {e}",
            )

        target_id: str | None = None
        for lock in locks:
            if device.cloud_id and lock.device_id == device.cloud_id:
                target_id = lock.device_id
                break
            if _slugify(lock.name) == device.entity_id:
                target_id = lock.device_id
                break

        if not target_id:
            return DeviceControlResult(
                success=False,
                entity_id=device.entity_id,
                action=action,
                error=f"Lock '{device.name}' not found on Schlage account",
            )

        try:
            if action == "lock":
                await asyncio.to_thread(client.lock, target_id)
            elif action == "unlock":
                await asyncio.to_thread(client.unlock, target_id)
            elif action == "get_status":
                refreshed = await asyncio.to_thread(client.refresh, target_id)
                return DeviceControlResult(
                    success=True,
                    entity_id=device.entity_id,
                    action=action,
                    extra={
                        "is_locked": refreshed.is_locked,
                        "is_jammed": refreshed.is_jammed,
                        "battery_level": refreshed.battery_level,
                    },
                )
            else:
                return DeviceControlResult(
                    success=False,
                    entity_id=device.entity_id,
                    action=action,
                    error=f"Unsupported action: {action}",
                )
        except Exception as e:
            return DeviceControlResult(
                success=False,
                entity_id=device.entity_id,
                action=action,
                error=f"Control failed: {e}",
            )

        return DeviceControlResult(
            success=True,
            entity_id=device.entity_id,
            action=action,
        )

    async def get_state(
        self, device: DiscoveredDevice
    ) -> dict[str, Any]:
        """Get current lock state, battery level, and jammed status."""
        username, password = _get_credentials()
        if not username or not password:
            return {"error": "Schlage credentials not configured"}

        try:
            client = await asyncio.to_thread(_build_schlage_client)
        except Exception as e:
            return {"error": f"Schlage auth failed: {e}"}

        # Find and refresh the target lock
        try:
            locks = await asyncio.to_thread(client.get_locks)
        except Exception as e:
            return {"error": f"Failed to list locks: {e}"}

        for lock in locks:
            if (device.cloud_id and lock.device_id == device.cloud_id) or \
               _slugify(lock.name) == device.entity_id:
                refreshed = await asyncio.to_thread(client.refresh, lock.device_id)
                return {
                    "is_locked": refreshed.is_locked,
                    "is_jammed": refreshed.is_jammed,
                    "battery_level": refreshed.battery_level,
                    "connected": refreshed.connected,
                }

        return {"error": f"Lock '{device.name}' not found"}
