"""Z-Wave JS Server service — singleton WebSocket client for device data and control.

Connects to Z-Wave JS Server's WebSocket API (port 3000 by default, enabled
in Z-Wave JS UI settings) to fetch node data and send control commands.
Cache is refreshed by the ZWaveAgent on a timer.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

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


from jarvis_command_sdk import JarvisStorage

logger = JarvisLogger(service="device.zwave")

_storage: JarvisStorage = JarvisStorage("zwave")

# Default cache staleness: 5 minutes
_DEFAULT_MAX_AGE_SECONDS: int = 300

# WebSocket timeout for operations
_WS_TIMEOUT_SECONDS: int = 30

# Z-Wave JS Server schema version to request
_SCHEMA_VERSION: int = 44

# Max WebSocket message size (node state dumps can be large)
_MAX_WS_SIZE: int = 10_000_000


# ---------------------------------------------------------------------------
# Z-Wave node → Jarvis domain classification
# ---------------------------------------------------------------------------

# Generic device class label → Jarvis domain
_GENERIC_CLASS_TO_DOMAIN: dict[str, str] = {
    "Binary Switch": "switch",
    "Binary Power Switch": "switch",
    "Multilevel Switch": "light",
    "Multilevel Power Switch": "light",
    "Door Lock": "lock",
    "Entry Control": "lock",
    "Thermostat": "climate",
    "General Thermostat": "climate",
    "Setback Thermostat": "climate",
    "Window Covering": "cover",
    "Barrier Operator": "cover",
}

# Command class → domain fallback (when generic class isn't mapped)
_CC_TO_DOMAIN: dict[int, str] = {
    37: "switch",   # Binary Switch
    38: "light",    # Multilevel Switch
    51: "light",    # Color Switch
    98: "lock",     # Door Lock
    64: "climate",  # Thermostat Mode
    67: "climate",  # Thermostat Setpoint
    102: "cover",   # Barrier Operator
}

# Priority order for domain classification (first match wins)
_DOMAIN_PRIORITY: list[str] = ["lock", "climate", "cover", "light", "switch"]


def classify_node(node: dict[str, Any]) -> str | None:
    """Determine the Jarvis domain for a Z-Wave node.

    Returns None for nodes that shouldn't be exposed (controllers, sensors-only).
    """
    if node.get("isControllerNode"):
        return None

    # Try generic device class first
    device_class: dict[str, Any] = node.get("deviceClass", {})
    specific_label: str = ""
    generic_label: str = ""

    generic_raw: Any = device_class.get("generic")
    specific_raw: Any = device_class.get("specific")
    if isinstance(generic_raw, dict):
        generic_label = generic_raw.get("label", "")
    if isinstance(specific_raw, dict):
        specific_label = specific_raw.get("label", "")

    for label in (specific_label, generic_label):
        domain: str | None = _GENERIC_CLASS_TO_DOMAIN.get(label)
        if domain:
            return domain

    # Fall back to command classes present in values
    values: Any = node.get("values", [])
    found_domains: set[str] = set()
    val_list: list[dict[str, Any]] = values if isinstance(values, list) else list(values.values()) if isinstance(values, dict) else []
    for val in val_list:
        cc: int | None = val.get("commandClass")
        if cc in _CC_TO_DOMAIN:
            found_domains.add(_CC_TO_DOMAIN[cc])

    for domain in _DOMAIN_PRIORITY:
        if domain in found_domains:
            return domain

    return None


def _iter_values(node: dict[str, Any]) -> list[dict[str, Any]]:
    """Get node values as a list regardless of whether the API returns a list or dict."""
    values: Any = node.get("values", [])
    if isinstance(values, list):
        return values
    if isinstance(values, dict):
        return list(values.values())
    return []


class ZWaveService:
    """Singleton service for Z-Wave JS Server communication.

    Connects to Z-Wave JS Server's WebSocket API for node discovery and
    control. The WS Server is enabled in Z-Wave JS UI under
    Settings → Z-Wave JS → WS Server (default port 3000).

    Usage:
        service = ZWaveService()
        await service.fetch_nodes()
        nodes = service.get_all_nodes()
        await service.set_value(2, 38, 0, "targetValue", 99)
    """

    _instance: Optional["ZWaveService"] = None

    def __new__(cls) -> "ZWaveService":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized: bool = True

        # Node cache: node_id → raw node data from Z-Wave JS Server
        self._nodes: dict[int, dict[str, Any]] = {}
        self._last_refresh: datetime | None = None
        self._last_error: str | None = None
        self._msg_counter: int = 0

    @staticmethod
    def _get_url() -> str | None:
        """Read the Z-Wave JS Server URL fresh each time (secret may be set after init)."""
        url: str | None = _storage.get_secret("ZWAVE_JS_URL")
        if url:
            logger.debug("Z-Wave JS URL resolved", url=url)
        else:
            logger.debug(
                "Z-Wave JS URL not found in JarvisStorage",
                storage_namespace=_storage._namespace,
                backend_type=type(_storage._backend).__name__ if _storage._backend else "None",
            )
        return url

    def _next_msg_id(self) -> str:
        self._msg_counter += 1
        return f"jarvis-{self._msg_counter}"

    async def refresh_if_stale(self, max_age_seconds: int = _DEFAULT_MAX_AGE_SECONDS) -> None:
        """Re-fetch Z-Wave data if cache is older than max_age_seconds."""
        if self._last_refresh is not None:
            age: float = (datetime.now(timezone.utc) - self._last_refresh).total_seconds()
            if age < max_age_seconds:
                logger.debug("Z-Wave cache still fresh", age_s=round(age, 1), max_age_s=max_age_seconds)
                return
            logger.debug("Z-Wave cache stale, refreshing", age_s=round(age, 1), max_age_s=max_age_seconds)
        else:
            logger.debug("Z-Wave cache empty, fetching for first time")
        await self.fetch_nodes()

    async def fetch_nodes(self) -> None:
        """Connect to Z-Wave JS Server and fetch all nodes via start_listening."""
        logger.info("Z-Wave fetch_nodes starting")

        url: str | None = self._get_url()
        if not url:
            self._last_error = "ZWAVE_JS_URL not configured"
            logger.warning("Z-Wave fetch skipped — no URL", reason=self._last_error)
            return

        try:
            import websockets
            logger.debug("websockets library loaded", version=getattr(websockets, "__version__", "unknown"))
        except ImportError:
            self._last_error = "websockets package not installed"
            logger.error("websockets package required for Z-Wave JS Server — pip install websockets")
            return

        logger.info("Z-Wave connecting to WS server", url=url)
        try:
            async with websockets.connect(
                url, max_size=_MAX_WS_SIZE, close_timeout=5,
            ) as ws:
                # 1. Read version handshake
                logger.debug("Z-Wave WS connected, waiting for version handshake")
                version_msg: dict[str, Any] = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_SECONDS)
                )
                logger.info(
                    "Z-Wave JS Server version",
                    server_version=version_msg.get("serverVersion"),
                    driver_version=version_msg.get("driverVersion"),
                    max_schema=version_msg.get("maxSchemaVersion"),
                )

                # 2. Initialize with schema version
                schema: int = min(
                    _SCHEMA_VERSION,
                    version_msg.get("maxSchemaVersion", _SCHEMA_VERSION),
                )
                init_id: str = self._next_msg_id()
                logger.debug("Z-Wave sending initialize", schema_version=schema)
                await ws.send(json.dumps({
                    "messageId": init_id,
                    "command": "initialize",
                    "schemaVersion": schema,
                }))
                init_resp: dict[str, Any] = await self._recv_for(ws, init_id)
                if not init_resp.get("success"):
                    raise ValueError(f"Initialize failed: {init_resp.get('message', '')}")
                logger.debug("Z-Wave initialize OK")

                # 3. Start listening → full state dump
                listen_id: str = self._next_msg_id()
                logger.debug("Z-Wave sending start_listening")
                await ws.send(json.dumps({
                    "messageId": listen_id,
                    "command": "start_listening",
                }))
                listen_resp: dict[str, Any] = await self._recv_for(ws, listen_id)
                if not listen_resp.get("success"):
                    raise ValueError(f"start_listening failed: {listen_resp.get('message', '')}")

                state: dict[str, Any] = listen_resp.get("result", {}).get("state", {})
                nodes_list: list[dict[str, Any]] = state.get("nodes", [])
                logger.info("Z-Wave state dump received", raw_node_count=len(nodes_list))

                self._nodes = {}
                for node in nodes_list:
                    node_id: int | None = node.get("nodeId")
                    if node_id is not None:
                        self._nodes[node_id] = node
                        is_controller: bool = node.get("isControllerNode", False)
                        domain: str | None = classify_node(node)
                        logger.debug(
                            "Z-Wave node loaded",
                            node_id=node_id,
                            name=node.get("name") or node.get("label"),
                            is_controller=is_controller,
                            classified_domain=domain,
                            status=node.get("status"),
                            value_count=len(node.get("values", [])),
                        )

                self._last_refresh = datetime.now(timezone.utc)
                self._last_error = None
                logger.info("Z-Wave nodes refreshed", count=len(self._nodes))

        except asyncio.TimeoutError:
            self._last_error = "Connection timeout"
            logger.error("Z-Wave JS Server connection timeout", url=url)
        except ConnectionRefusedError:
            self._last_error = "Connection refused — is Z-Wave JS Server running?"
            logger.error("Z-Wave JS Server connection refused", url=url)
        except Exception as e:
            self._last_error = str(e)
            logger.error("Z-Wave fetch error", error=str(e), error_type=type(e).__name__)

    async def set_value(
        self,
        node_id: int,
        command_class: int,
        endpoint: int,
        property_name: str,
        value: Any,
        property_key: int | str | None = None,
    ) -> tuple[bool, str | None]:
        """Send a node.set_value command to Z-Wave JS Server.

        Args:
            node_id: Z-Wave node ID.
            command_class: Z-Wave command class number.
            endpoint: Endpoint index (usually 0).
            property_name: Value property name (e.g., "targetValue").
            value: The value to set.
            property_key: Optional property key (needed for thermostat setpoints).

        Returns:
            (True, None) on success, (False, error_message) on failure.
        """
        url: str | None = self._get_url()
        if not url:
            logger.error("ZWAVE_JS_URL not configured")
            return False, "ZWAVE_JS_URL not configured"

        try:
            import websockets
        except ImportError:
            logger.error("websockets package not installed")
            return False, "websockets package not installed"

        value_id: dict[str, Any] = {
            "commandClass": command_class,
            "endpoint": endpoint,
            "property": property_name,
        }
        if property_key is not None:
            value_id["propertyKey"] = property_key

        try:
            async with websockets.connect(
                url, max_size=_MAX_WS_SIZE, close_timeout=5,
            ) as ws:
                # Handshake: version → initialize
                version_msg = json.loads(
                    await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_SECONDS)
                )
                init_id: str = self._next_msg_id()
                await ws.send(json.dumps({
                    "messageId": init_id,
                    "command": "initialize",
                    "schemaVersion": min(
                        _SCHEMA_VERSION,
                        version_msg.get("maxSchemaVersion", _SCHEMA_VERSION),
                    ),
                }))
                await self._recv_for(ws, init_id)

                # Send set_value
                set_id: str = self._next_msg_id()
                await ws.send(json.dumps({
                    "messageId": set_id,
                    "command": "node.set_value",
                    "nodeId": node_id,
                    "valueId": value_id,
                    "value": value,
                }))
                resp: dict[str, Any] = await self._recv_for(ws, set_id)

                success: bool = resp.get("success", False)
                if success:
                    logger.info(
                        "Z-Wave value set",
                        node_id=node_id, cc=command_class,
                        prop=property_name, value=value,
                    )
                    return True, None

                error_msg: str = resp.get("message") or str(resp)[:200]
                logger.error(
                    "Z-Wave set_value failed",
                    node_id=node_id,
                    error=error_msg,
                )
                return False, error_msg

        except Exception as e:
            logger.error("Z-Wave set_value error", error=str(e), node_id=node_id)
            return False, str(e)

    # ------------------------------------------------------------------
    # Cache accessors
    # ------------------------------------------------------------------

    def get_node(self, node_id: int) -> dict[str, Any] | None:
        """Get cached node data by ID."""
        return self._nodes.get(node_id)

    def get_all_nodes(self) -> dict[int, dict[str, Any]]:
        """Get all cached nodes."""
        return self._nodes

    def get_context_data(self) -> dict[str, Any]:
        """Return cached Z-Wave data for voice request context."""
        devices: list[dict[str, Any]] = []
        for node_id, node in self._nodes.items():
            domain: str | None = classify_node(node)
            if domain is None:
                continue

            name: str = node.get("name") or node.get("label") or f"Node {node_id}"
            location: str = node.get("location", "")

            device_info: dict[str, Any] = {
                "entity_id": f"{domain}.zwave_node_{node_id}",
                "name": name,
                "domain": domain,
                "state": self._get_node_state_summary(node, domain),
            }
            if location:
                device_info["area"] = location

            devices.append(device_info)

        return {
            "devices": devices,
            "node_count": len(self._nodes),
            "last_refresh": self._last_refresh.isoformat() if self._last_refresh else None,
            "last_error": self._last_error,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _recv_for(ws: Any, message_id: str, max_attempts: int = 50) -> dict[str, Any]:
        """Read WebSocket messages until we get one matching our message ID."""
        for _ in range(max_attempts):
            raw: str = await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT_SECONDS)
            msg: dict[str, Any] = json.loads(raw)
            if msg.get("messageId") == message_id:
                return msg
        raise ValueError(f"No response for messageId={message_id} after {max_attempts} reads")

    @staticmethod
    def _get_node_state_summary(node: dict[str, Any], domain: str) -> str:
        """Extract a human-readable state from cached node values."""
        values: list[dict[str, Any]] = _iter_values(node)

        if domain == "switch":
            for val in values:
                if val.get("commandClass") == 37 and val.get("property") == "currentValue":
                    return "on" if val.get("value") else "off"

        elif domain == "light":
            for val in values:
                if val.get("commandClass") == 38 and val.get("property") == "currentValue":
                    level: int = val.get("value", 0)
                    return "off" if level == 0 else f"on ({level}%)"

        elif domain == "lock":
            for val in values:
                if val.get("commandClass") == 98 and val.get("property") == "currentMode":
                    mode: Any = val.get("value")
                    if mode == 255:
                        return "locked"
                    if mode == 0:
                        return "unlocked"
                    return str(mode)

        elif domain == "climate":
            for val in values:
                if val.get("commandClass") == 67 and val.get("property") == "setpoint":
                    temp: Any = val.get("value")
                    if temp is not None:
                        return f"setpoint {temp}"

        elif domain == "cover":
            for val in values:
                if val.get("commandClass") == 38 and val.get("property") == "currentValue":
                    pos: int = val.get("value", 0)
                    if pos == 0:
                        return "closed"
                    if pos >= 99:
                        return "open"
                    return f"open ({pos}%)"

        status: Any = node.get("status")
        if status == 0:  # dead
            return "offline"
        return "unknown"
