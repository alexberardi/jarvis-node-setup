"""Z-Wave device protocol adapter via Z-Wave JS Server WebSocket API."""

from __future__ import annotations

import re
from typing import Any

from jarvis_command_sdk import (
    DeviceControlResult,
    DiscoveredDevice,
    IJarvisButton,
    IJarvisDeviceProtocol,
    IJarvisSecret,
    JarvisSecret,
)

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
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


logger = JarvisLogger(service="device.zwave")


def _slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _parse_node_id_from_entity(entity_id: str) -> int | None:
    """Extract Z-Wave node ID from entity_id (e.g., 'zw6hd_2' → 2).

    Entity IDs are created as '{slugified_name}_{node_id}' by discover().
    """
    try:
        suffix: str = entity_id.rsplit("_", 1)[-1]
        return int(suffix)
    except (ValueError, IndexError):
        logger.warning("Could not parse node_id from entity_id", entity_id=entity_id)
        return None


# ---------------------------------------------------------------------------
# Action → Z-Wave CC mapping
# ---------------------------------------------------------------------------

def _resolve_action(
    domain: str,
    action: str,
    params: dict[str, Any] | None = None,
    node: dict[str, Any] | None = None,
) -> tuple[int, int, str, Any, int | str | None] | None:
    """Map a Jarvis action to Z-Wave writeValue args.

    Returns (command_class, endpoint, property, value, property_key)
    or None if the action isn't supported.
    """
    params = params or {}

    if domain == "switch":
        if action == "turn_on":
            return (37, 0, "targetValue", True, None)
        if action == "turn_off":
            return (37, 0, "targetValue", False, None)
        if action == "toggle":
            current: bool = _read_current_bool(node, 37)
            return (37, 0, "targetValue", not current, None)

    elif domain == "light":
        if action == "turn_on":
            brightness: int | None = params.get("brightness")
            return (38, 0, "targetValue", brightness if brightness is not None else 99, None)
        if action == "turn_off":
            return (38, 0, "targetValue", 0, None)
        if action == "set_brightness":
            level: int = max(0, min(99, int(params.get("brightness", 99))))
            return (38, 0, "targetValue", level, None)
        if action == "toggle":
            cur_level: int = _read_current_level(node, 38)
            return (38, 0, "targetValue", 0 if cur_level > 0 else 99, None)

    elif domain == "lock":
        if action == "lock":
            return (98, 0, "targetMode", 255, None)
        if action == "unlock":
            return (98, 0, "targetMode", 0, None)

    elif domain == "climate":
        if action == "set_temperature":
            temp: Any = params.get("temperature")
            if temp is None:
                return None
            # Find the active setpoint property key from the node's values
            prop_key: int | str | None = _find_setpoint_key(node)
            return (67, 0, "setpoint", float(temp), prop_key)
        if action == "set_hvac_mode":
            mode: Any = params.get("mode")
            if mode is None:
                return None
            return (64, 0, "mode", int(mode), None)

    elif domain == "cover":
        if action == "open_cover":
            return (38, 0, "targetValue", 99, None)
        if action == "close_cover":
            return (38, 0, "targetValue", 0, None)
        if action == "stop_cover":
            return (38, 0, "targetValue", 50, None)

    return None


def _iter_values(node: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Get node values as a list (API returns list, older versions may return dict)."""
    if not node:
        return []
    values: Any = node.get("values", [])
    if isinstance(values, list):
        return values
    if isinstance(values, dict):
        return list(values.values())
    return []


def _read_current_bool(node: dict[str, Any] | None, cc: int) -> bool:
    """Read a boolean currentValue from cached node data."""
    for val in _iter_values(node):
        if val.get("commandClass") == cc and val.get("property") == "currentValue":
            return bool(val.get("value", False))
    return False


def _read_current_level(node: dict[str, Any] | None, cc: int) -> int:
    """Read an integer currentValue from cached node data."""
    for val in _iter_values(node):
        if val.get("commandClass") == cc and val.get("property") == "currentValue":
            return int(val.get("value", 0))
    return 0


def _find_setpoint_key(node: dict[str, Any] | None) -> int | str | None:
    """Find the first thermostat setpoint property key (Heating=1, Cooling=2)."""
    for val in _iter_values(node):
        if val.get("commandClass") == 67 and val.get("property") == "setpoint":
            pk: Any = val.get("propertyKey")
            if pk is not None:
                return pk
    return 1  # Default to Heating


# ---------------------------------------------------------------------------
# State extraction
# ---------------------------------------------------------------------------

def _extract_state(node: dict[str, Any], domain: str) -> dict[str, Any]:
    """Extract device state from cached node values."""
    values: list[dict[str, Any]] = _iter_values(node)
    state: dict[str, Any] = {}

    for val in values:
        cc: int | None = val.get("commandClass")
        prop: str = val.get("property", "")
        v: Any = val.get("value")

        if domain == "switch" and cc == 37 and prop == "currentValue":
            state["state"] = "on" if v else "off"

        elif domain == "light" and cc == 38 and prop == "currentValue":
            if v == 0:
                state["state"] = "off"
            else:
                state["state"] = "on"
                state["brightness"] = v

        elif domain == "lock" and cc == 98 and prop == "currentMode":
            if v == 255:
                state["state"] = "locked"
            elif v == 0:
                state["state"] = "unlocked"
            else:
                state["state"] = f"mode_{v}"

        elif domain == "climate":
            if cc == 67 and prop == "setpoint":
                state["target_temperature"] = v
            elif cc == 49 and prop == "Air temperature":
                state["current_temperature"] = v
            elif cc == 64 and prop == "mode":
                state["hvac_mode"] = v

        elif domain == "cover" and cc == 38 and prop == "currentValue":
            if v == 0:
                state["state"] = "closed"
            elif v >= 99:
                state["state"] = "open"
            else:
                state["state"] = "open"
                state["position"] = v

        # Battery (common across domains)
        if cc == 128 and prop == "level":
            state["battery"] = v

    if "state" not in state:
        status: Any = node.get("status")
        state["state"] = "offline" if status == 0 else "unknown"

    return state


# ---------------------------------------------------------------------------
# Protocol adapter
# ---------------------------------------------------------------------------

class ZWaveProtocol(IJarvisDeviceProtocol):
    """Z-Wave device protocol via Z-Wave JS Server WebSocket API."""

    protocol_name: str = "zwave"
    friendly_name: str = "Z-Wave"
    supported_domains: list[str] = ["switch", "light", "lock", "climate", "cover"]
    connection_type: str = "lan"

    setup_guide: str = """## Setup

1. Install a Z-Wave USB stick (e.g., Zooz ZST39, Aeotec Z-Stick 7)
2. Run **Z-Wave JS UI** as a container with the USB stick passed through
3. Enable the **WS Server** in Z-Wave JS UI settings (default port 3000)
4. Pair your Z-Wave devices using the Z-Wave JS UI web interface
5. Set the **Z-Wave JS Server URL** below (e.g., `ws://10.0.0.244:3000`)
6. Tap **Scan for Devices** to discover paired devices"""

    @property
    def required_secrets(self) -> list[IJarvisSecret]:
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

    @property
    def supported_actions(self) -> list[IJarvisButton]:
        return [
            IJarvisButton(button_text="Turn On", button_action="turn_on", button_type="primary", button_icon="power"),
            IJarvisButton(button_text="Turn Off", button_action="turn_off", button_type="secondary", button_icon="power-off"),
            IJarvisButton(button_text="Lock", button_action="lock", button_type="primary", button_icon="lock"),
            IJarvisButton(button_text="Unlock", button_action="unlock", button_type="secondary", button_icon="lock-open-variant"),
        ]

    async def discover(self, timeout: int = 5) -> list[DiscoveredDevice]:
        logger.info("Z-Wave discover() called", timeout=timeout)

        try:
            from .zwave_service import ZWaveService, classify_node
            logger.debug("Z-Wave service import OK")
        except ImportError as e:
            logger.error("Z-Wave service import failed", error=str(e))
            return []

        service: ZWaveService = ZWaveService()
        logger.debug(
            "ZWaveService state before refresh",
            cached_nodes=len(service.get_all_nodes()),
            last_error=service._last_error,
        )

        await service.refresh_if_stale(max_age_seconds=0)  # Force fresh data

        all_nodes: dict[int, Any] = service.get_all_nodes()
        logger.info("Z-Wave nodes after refresh", node_count=len(all_nodes), last_error=service._last_error)

        devices: list[DiscoveredDevice] = []
        for node_id, node in all_nodes.items():
            domain: str | None = classify_node(node)
            if domain is None:
                logger.debug("Z-Wave node skipped (no domain)", node_id=node_id, is_controller=node.get("isControllerNode"))
                continue

            name: str = node.get("name") or node.get("label") or f"Node {node_id}"
            location: str = node.get("location", "")

            devices.append(
                DiscoveredDevice(
                    entity_id=f"{_slugify(name)}_{node_id}",
                    name=name,
                    domain=domain,
                    protocol=self.protocol_name,
                    manufacturer=node.get("manufacturer", ""),
                    model=node.get("label", ""),
                    extra={
                        "node_id": node_id,
                        "location": location,
                        "firmware": node.get("firmwareVersion", ""),
                        "status": node.get("status", "unknown"),
                    },
                )
            )
            logger.debug("Z-Wave device discovered", node_id=node_id, name=name, domain=domain)

        logger.info(f"Z-Wave discovery complete: {len(devices)} device(s) from {len(all_nodes)} node(s)")
        return devices

    async def control(
        self, device: DiscoveredDevice, action: str, params: dict[str, Any] | None = None,
    ) -> DeviceControlResult:
        from .zwave_service import ZWaveService, classify_node

        service: ZWaveService = ZWaveService()
        node_id: int | None = device.extra.get("node_id") if device.extra else None
        if node_id is None:
            node_id = _parse_node_id_from_entity(device.entity_id)
        if node_id is None:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error="Missing node_id in device data",
            )

        # Read cached node for toggle/thermostat logic
        node: dict[str, Any] | None = service.get_node(node_id)

        # Use classified domain from node cache (CC may not send domain in context)
        domain: str = device.domain
        if node and domain == "switch":
            classified: str | None = classify_node(node)
            if classified:
                domain = classified
                logger.debug("Domain resolved from node cache", node_id=node_id, domain=domain)

        resolved: tuple[int, int, str, Any, int | str | None] | None = _resolve_action(
            domain, action, params, node,
        )
        if resolved is None:
            return DeviceControlResult(
                success=False, entity_id=device.entity_id, action=action,
                error=f"Unsupported action '{action}' for domain '{device.domain}'",
            )

        cc, endpoint, prop, value, prop_key = resolved
        success, error_msg = await service.set_value(
            node_id, cc, endpoint, prop, value, property_key=prop_key,
        )

        return DeviceControlResult(
            success=success, entity_id=device.entity_id, action=action,
            error=error_msg,
        )

    async def get_state(self, device: DiscoveredDevice) -> dict[str, Any]:
        from .zwave_service import ZWaveService

        service: ZWaveService = ZWaveService()
        node_id: int | None = device.extra.get("node_id") if device.extra else None
        if node_id is None:
            node_id = _parse_node_id_from_entity(device.entity_id)
        if node_id is None:
            return {"error": "Missing node_id in device data"}

        node: dict[str, Any] | None = service.get_node(node_id)
        if node is None:
            return {"error": f"Node {node_id} not in cache — run discovery first"}

        return _extract_state(node, device.domain)
