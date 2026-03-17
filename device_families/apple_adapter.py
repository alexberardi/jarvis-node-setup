"""Apple device protocol adapter (Apple TV, HomePod).

Uses the pyatv library for mDNS-based discovery and AirPlay/Companion
protocol control. Apple devices advertise via Bonjour and support
remote control via MRP (Media Remote Protocol) and Companion.

Install: pip install pyatv
"""

import asyncio
import re
from typing import Any

from jarvis_log_client import JarvisLogger

from core.ijarvis_button import IJarvisButton
from device_families.base import (
    DeviceControlResult,
    DeviceProtocol,
    DiscoveredDevice,
)

logger = JarvisLogger(service="jarvis-node")

# pyatv DeviceModel values we consider valid smart home devices.
# Excludes Macs, iPhones, iPads, and third-party AirPlay receivers.
_SUPPORTED_MODELS: set[str] = {
    "DeviceModel.AppleTV4K",
    "DeviceModel.AppleTV4KGen2",
    "DeviceModel.AppleTV4KGen3",
    "DeviceModel.AppleTV4KGen4",
    "DeviceModel.AppleTV4Gen",
    "DeviceModel.AppleTVGen4",
    "DeviceModel.Gen4K",
    "DeviceModel.Gen4KGen2",
    "DeviceModel.HomePod",
    "DeviceModel.HomePodGen2",
    "DeviceModel.HomePodMini",
}

# Raw model prefixes for fallback when DeviceModel is Unknown
_SUPPORTED_RAW_PREFIXES: list[str] = [
    "AppleTV",
    "AudioAccessory",  # HomePod family
]

_FRIENDLY_NAMES: dict[str, str] = {
    "DeviceModel.AppleTV4K": "Apple TV 4K",
    "DeviceModel.AppleTV4KGen2": "Apple TV 4K (2nd gen)",
    "DeviceModel.AppleTV4KGen3": "Apple TV 4K (3rd gen)",
    "DeviceModel.AppleTV4KGen4": "Apple TV 4K (4th gen)",
    "DeviceModel.AppleTV4Gen": "Apple TV (4th gen)",
    "DeviceModel.AppleTVGen4": "Apple TV (4th gen)",
    "DeviceModel.Gen4K": "Apple TV 4K",
    "DeviceModel.Gen4KGen2": "Apple TV 4K (2nd gen)",
    "DeviceModel.HomePod": "HomePod",
    "DeviceModel.HomePodGen2": "HomePod (2nd gen)",
    "DeviceModel.HomePodMini": "HomePod mini",
}


def _slugify(name: str) -> str:
    """Convert device name to HA-style entity slug."""
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _is_supported_device(model_str: str | None, raw_model: str | None) -> bool:
    """Check if a pyatv device is an Apple TV or HomePod (not a Mac/third-party)."""
    if model_str and model_str in _SUPPORTED_MODELS:
        return True
    if raw_model:
        return any(raw_model.startswith(p) for p in _SUPPORTED_RAW_PREFIXES)
    return False


def _model_to_device_class(model_str: str | None) -> str | None:
    """Map Apple model identifier to a device class hint."""
    if not model_str:
        return None
    lower = model_str.lower()
    if "homepod" in lower or "audioaccessory" in lower:
        return "speaker"
    if "tv" in lower or "appletv" in lower:
        return "tv"
    return None


def _model_friendly(model_str: str | None, raw_model: str | None) -> str:
    """Best-effort friendly model name."""
    if model_str and model_str in _FRIENDLY_NAMES:
        return _FRIENDLY_NAMES[model_str]
    if raw_model:
        if raw_model.startswith("AudioAccessory"):
            return "HomePod"
        if raw_model.startswith("AppleTV"):
            return "Apple TV"
    return "Apple Device"


class AppleProtocol(DeviceProtocol):
    """Apple AirPlay/Companion protocol: discovery + control over LAN."""

    @property
    def protocol_name(self) -> str:
        return "apple"

    @property
    def friendly_name(self) -> str:
        return "Apple"

    @property
    def description(self) -> str:
        return "Apple TV and HomePod devices"

    @property
    def supported_domains(self) -> list[str]:
        return ["media_player"]

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton("Play", "play", "primary", "play"),
            IJarvisButton("Pause", "pause", "secondary", "pause"),
            IJarvisButton("Power On", "turn_on", "primary", "power"),
            IJarvisButton("Power Off", "turn_off", "destructive", "power-off"),
            IJarvisButton("Vol Up", "volume_up", "secondary", "volume-plus"),
            IJarvisButton("Vol Down", "volume_down", "secondary", "volume-minus"),
        ]

    async def discover(self, timeout: float = 5.0) -> list[DiscoveredDevice]:
        """Discover Apple TV and HomePod devices via mDNS/Bonjour."""
        try:
            import pyatv
        except ImportError:
            logger.warning("pyatv not installed, skipping Apple discovery")
            return []

        try:
            loop = asyncio.get_event_loop()
            atvs = await pyatv.scan(loop, timeout=timeout)
        except Exception as e:
            logger.error("Apple device scan failed", error=str(e))
            return []

        # Deduplicate by MAC (device may appear on multiple interfaces)
        seen_macs: set[str] = set()
        results: list[DiscoveredDevice] = []

        for atv in atvs:
            try:
                info = atv.device_info
                model_str = str(info.model) if info and info.model else None
                raw_model = str(info.raw_model) if info and info.raw_model else None

                # Skip non-Apple-TV/HomePod devices (Macs, Denon, etc.)
                if not _is_supported_device(model_str, raw_model):
                    logger.debug(
                        "Skipping non-smart-home Apple device",
                        name=atv.name,
                        model=model_str,
                        raw_model=raw_model,
                    )
                    continue

                mac = str(info.mac) if info and info.mac else None
                if mac:
                    if mac in seen_macs:
                        continue
                    seen_macs.add(mac)

                name = atv.name or "Apple Device"
                ip = str(atv.address)
                identifier = str(atv.identifier) if atv.identifier else None
                os_name = str(info.operating_system) if info and info.operating_system else None
                os_version = str(info.version) if info and info.version else None

                device_class = _model_to_device_class(model_str or raw_model)
                friendly = _model_friendly(model_str, raw_model)

                slug = _slugify(name)
                entity_id = f"media_player.{slug}"

                results.append(DiscoveredDevice(
                    name=name,
                    domain="media_player",
                    manufacturer="Apple",
                    model=friendly,
                    protocol="apple",
                    local_ip=ip,
                    mac_address=mac,
                    entity_id=entity_id,
                    device_class=device_class,
                    is_controllable=True,
                    extra={
                        "identifier": identifier,
                        "os_name": os_name,
                        "os_version": os_version,
                        "raw_model": raw_model,
                        "services": [str(s.protocol) for s in atv.services],
                    },
                ))
                logger.debug("Found Apple device", name=name, ip=ip, model=friendly)
            except Exception as e:
                logger.warning(
                    "Failed to query Apple device",
                    error=str(e),
                    device=getattr(atv, "name", "unknown"),
                )

        return results

    async def control(
        self,
        ip: str,
        action: str,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> DeviceControlResult:
        """Control an Apple device.

        Connects via pyatv and sends remote control commands.
        Note: Some actions require the device to be paired first.
        """
        try:
            import pyatv
            from pyatv import exceptions
        except ImportError:
            return DeviceControlResult(
                success=False,
                entity_id=kwargs.get("entity_id", ""),
                action=action,
                error="pyatv not installed",
            )

        entity_id = kwargs.get("entity_id", f"media_player.{ip}")

        try:
            loop = asyncio.get_event_loop()
            atvs = await pyatv.scan(loop, hosts=[ip], timeout=5)
            if not atvs:
                return DeviceControlResult(
                    success=False,
                    entity_id=entity_id,
                    action=action,
                    error=f"Apple device not found at {ip}",
                )

            config = atvs[0]
            atv = await pyatv.connect(config, loop)

            try:
                rc = atv.remote_control
                if action == "turn_on":
                    try:
                        await atv.power.turn_on()
                    except exceptions.NotSupportedError:
                        try:
                            await rc.wakeup()
                        except exceptions.NotSupportedError:
                            return DeviceControlResult(
                                success=False, entity_id=entity_id, action=action,
                                error="Power on not supported by this device",
                            )
                elif action == "turn_off":
                    try:
                        await atv.power.turn_off()
                    except exceptions.NotSupportedError:
                        try:
                            await rc.suspend()
                        except exceptions.NotSupportedError:
                            return DeviceControlResult(
                                success=False, entity_id=entity_id, action=action,
                                error="Power off not supported by this device",
                            )
                elif action == "play":
                    await rc.play()
                elif action == "pause":
                    await rc.pause()
                elif action == "play_pause":
                    await rc.play_pause()
                elif action == "stop":
                    await rc.stop()
                elif action == "next":
                    await rc.next()
                elif action == "previous":
                    await rc.previous()
                elif action == "volume_up":
                    await rc.volume_up()
                elif action == "volume_down":
                    await rc.volume_down()
                elif action == "set_volume":
                    if data and "volume" in data:
                        await rc.set_volume(float(data["volume"]))
                    else:
                        return DeviceControlResult(
                            success=False,
                            entity_id=entity_id,
                            action=action,
                            error="set_volume requires 'volume' in data (0-100)",
                        )
                elif action == "select":
                    await rc.select()
                elif action == "menu":
                    await rc.menu()
                elif action == "home":
                    await rc.home()
                else:
                    return DeviceControlResult(
                        success=False,
                        entity_id=entity_id,
                        action=action,
                        error=f"Unsupported action: {action}",
                    )

                return DeviceControlResult(
                    success=True, entity_id=entity_id, action=action,
                )
            finally:
                atv.close()

        except Exception as e:
            error_msg = str(e)
            if "not paired" in error_msg.lower() or "auth" in error_msg.lower():
                error_msg = f"Device requires pairing: {error_msg}"
            return DeviceControlResult(
                success=False,
                entity_id=entity_id,
                action=action,
                error=error_msg,
            )

    async def get_state(self, ip: str, **kwargs: Any) -> dict[str, Any] | None:
        """Query Apple device state (playing, idle, etc.)."""
        try:
            import pyatv
        except ImportError:
            return None

        try:
            loop = asyncio.get_event_loop()
            atvs = await pyatv.scan(loop, hosts=[ip], timeout=5)
            if not atvs:
                return None

            config = atvs[0]
            atv = await pyatv.connect(config, loop)

            try:
                playing = atv.metadata
                state = await playing.playing()

                # Map pyatv DeviceState to on/off/playing/paused/idle
                state_map = {
                    "DeviceState.Idle": "idle",
                    "DeviceState.Playing": "playing",
                    "DeviceState.Paused": "paused",
                    "DeviceState.Loading": "loading",
                    "DeviceState.Stopped": "idle",
                    "DeviceState.Seeking": "playing",
                }
                device_state = state_map.get(str(state.device_state), "unknown")

                result: dict[str, Any] = {
                    "state": "on" if device_state != "idle" else "off",
                    "media_state": device_state,
                }

                if state.title:
                    result["media_title"] = state.title
                if state.artist:
                    result["media_artist"] = state.artist
                if state.album:
                    result["media_album"] = state.album
                if state.media_type:
                    result["media_type"] = str(state.media_type)

                return result
            finally:
                atv.close()

        except Exception as e:
            logger.debug("Failed to get Apple device state", ip=ip, error=str(e))
            return None
