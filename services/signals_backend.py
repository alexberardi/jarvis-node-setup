"""Concrete SignalsBackend for the node runtime.

Bridges the SDK's JarvisSignals producer facade to command-center's Signal Bus
ingress (``POST {cc}/api/v0/signals``). CC resolves household_id from the
authenticated node — never set it here. The SDK builds the full request body
({"signal": {...}, "data": ..., optional "command": ...}); this just posts it.

Registered once at startup via:
    from services.signals_backend import init_signals_backend
    init_signals_backend()
"""

from __future__ import annotations

from typing import Any

from jarvis_command_sdk.signals import SignalsBackend, set_signals_backend

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


class NodeSignalsBackend(SignalsBackend):
    """Signals backend that POSTs to command-center on behalf of the node.

    Returns discriminated string tags (never raises) so the facade's callers can
    map each failure mode — same shape as NodeInboxBackend:
        "ok"           — accepted by command-center
        "import_error" — clients.rest_client / utils.service_discovery missing
        "no_cc_url"    — service discovery returned no command-center URL
        "http_error"   — the POST failed (timeout, non-2xx, unreachable)
    """

    def emit_signal(self, payload: dict[str, Any]) -> str:
        # Deferred imports so a broken runtime degrades to a tag instead of an
        # exception propagating into a community package.
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url
        except ImportError as e:
            logger.error("signal emit import failed", error=str(e))
            return "import_error"

        cc_url = get_command_center_url()
        if not cc_url:
            logger.error("signal emit: get_command_center_url returned empty")
            return "no_cc_url"

        try:
            result = RestClient.post(
                f"{cc_url}/api/v0/signals",
                data=payload,
                timeout=10,
            )
        except Exception as e:  # noqa: BLE001 — never raise into a community package
            logger.error("signal emit POST raised", error=str(e))
            return "http_error"
        if result is None:
            return "http_error"
        return "ok"


def init_signals_backend() -> None:
    """Register the node signals backend with the SDK. Call once at startup."""
    set_signals_backend(NodeSignalsBackend())
