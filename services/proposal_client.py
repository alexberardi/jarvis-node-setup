"""Fetch a user's proposable-action suppressions ('never suggest this again')
from command-center, for detector agents to consult.

Mirrors services.node_llm_client: resolve the CC URL via service discovery and
call it with the node's X-API-Key (RestClient adds it). FAIL-OPEN by design —
any error returns empty signals, because a transient fetch miss must never stop
the agent from making suggestions (worst case a blocked item reappears once).
"""

from urllib.parse import urlencode

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

_EMPTY = {"source_keys": [], "descriptors": []}


def get_suppression_signals(command: str, user_id: int) -> dict:
    """Return ``{"source_keys": [...], "descriptors": [...]}`` for (this node's
    household, user_id, command). Empty on any failure (fail-open)."""
    cc_url = (get_command_center_url() or "").rstrip("/")
    if not cc_url:
        return dict(_EMPTY)
    qs = urlencode({"command": command, "user_id": user_id})
    try:
        result = RestClient.get(f"{cc_url}/api/v0/proposals/suppressions?{qs}")
    except Exception as e:  # noqa: BLE001 — fail-open
        logger.warning("suppression signals fetch failed", error=str(e))
        return dict(_EMPTY)
    if not isinstance(result, dict):
        return dict(_EMPTY)
    return {
        "source_keys": list(result.get("source_keys") or []),
        "descriptors": list(result.get("descriptors") or []),
    }
