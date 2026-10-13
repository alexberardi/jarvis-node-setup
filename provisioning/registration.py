"""
Command center registration for newly provisioned nodes.

Uses provisioning tokens (short-lived, single-use) instead of admin API keys.
The command center generates the node UUID at token creation time, and the
mobile app passes both the UUID and token to the node during provisioning.
"""

import time
from dataclasses import dataclass

import httpx
from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

# Registration runs seconds after the WiFi join, and `nmcli connection up`
# returns on association — DHCP/routes/DNS can settle a few seconds later.
# A connect-level failure in that window is transient, so retry over ~30s
# before declaring the provisioning attempt failed.
_CONNECT_RETRIES = 8
_CONNECT_RETRY_DELAY_S = 4.0

# A 5xx means CC (or a proxy in front of it) is up but momentarily unhappy —
# worth a few retries. A 4xx is a definitive answer (bad/expired token,
# unknown node...) and is never retried: the same token will fail the same way.
_SERVER_ERROR_RETRIES = 3
_SERVER_ERROR_RETRY_DELAY_S = 4.0

# Failure kinds reported in RegistrationResult.error_kind.
KIND_TOKEN_REJECTED = "token_rejected"   # 401/403 — token invalid/expired/used
KIND_REJECTED = "rejected"               # other 4xx
KIND_SERVER_ERROR = "server_error"       # 5xx after retries
KIND_NETWORK_ERROR = "network_error"     # connect/timeout after retries
KIND_BAD_RESPONSE = "bad_response"       # 2xx without usable credentials


@dataclass(frozen=True)
class RegistrationResult:
    """Outcome of a registration attempt, with enough detail to show a user."""

    ok: bool
    node_id: str | None = None
    node_key: str | None = None
    error_kind: str | None = None
    status_code: int | None = None
    detail: str | None = None

    @property
    def reason(self) -> str:
        """Human-readable failure reason (empty on success)."""
        if self.ok:
            return ""
        if self.status_code is not None:
            base = f"Command center rejected registration (HTTP {self.status_code})"
            return f"{base}: {self.detail}" if self.detail else base
        if self.error_kind == KIND_NETWORK_ERROR:
            msg = "Could not reach command center"
            return f"{msg}: {self.detail}" if self.detail else msg
        return self.detail or "Registration failed"


def _error_detail(response: httpx.Response) -> str | None:
    """Pull CC's ``detail`` (FastAPI convention) out of an error response."""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("error") or body.get("message")
        if isinstance(detail, str) and detail:
            return detail[:200]
    text = response.text
    if isinstance(text, str) and text.strip():
        return text.strip()[:200]
    return None


def register_node(
    command_center_url: str,
    node_id: str,
    provisioning_token: str,
    room: str | None = None,
) -> RegistrationResult:
    """
    Register this node with the command center using a provisioning token.

    Retries network errors (~30s) and 5xx (3 attempts); 4xx fails at once.

    Args:
        command_center_url: Base URL of the command center (e.g., http://192.168.1.50:7703)
        node_id: CC-assigned UUID for this node
        provisioning_token: Short-lived provisioning token from command center
        room: Room name for this node (optional)

    Returns:
        RegistrationResult — ``ok`` with credentials, or the failure kind,
        HTTP status and CC's error detail.
    """
    url = f"{command_center_url.rstrip('/')}/api/v0/nodes/register"
    payload: dict = {
        "node_id": node_id,
        "provisioning_token": provisioning_token,
    }
    if room is not None:
        payload["room"] = room

    logger.info("Registering with command center", url=url, node_id=node_id)

    network_attempts = 0
    server_attempts = 0
    while True:
        try:
            with httpx.Client(timeout=30.0) as client:
                response = client.post(url, json=payload)
        except httpx.RequestError as e:
            network_attempts += 1
            if network_attempts < _CONNECT_RETRIES:
                logger.warning(
                    "Registration request failed, retrying",
                    error=str(e),
                    attempt=network_attempts,
                    retries_left=_CONNECT_RETRIES - network_attempts,
                )
                time.sleep(_CONNECT_RETRY_DELAY_S)
                continue
            logger.error("Registration request failed", error=str(e))
            return RegistrationResult(
                ok=False, error_kind=KIND_NETWORK_ERROR, detail=str(e)[:200] or None
            )

        status = response.status_code
        if status in (200, 201):
            try:
                data = response.json()
            except ValueError:
                data = None
            if not isinstance(data, dict) or not data.get("node_key"):
                logger.error("Registration response missing node credentials", status=status)
                return RegistrationResult(
                    ok=False,
                    error_kind=KIND_BAD_RESPONSE,
                    detail="Command center did not return node credentials",
                )
            logger.info("Registration successful", node_id=data.get("node_id"))
            return RegistrationResult(
                ok=True, node_id=data.get("node_id"), node_key=data.get("node_key")
            )

        detail = _error_detail(response)
        if status >= 500:
            server_attempts += 1
            if server_attempts < _SERVER_ERROR_RETRIES:
                logger.warning(
                    "Registration got server error, retrying",
                    status=status,
                    attempt=server_attempts,
                    retries_left=_SERVER_ERROR_RETRIES - server_attempts,
                )
                time.sleep(_SERVER_ERROR_RETRY_DELAY_S)
                continue
            kind = KIND_SERVER_ERROR
        elif status in (401, 403):
            kind = KIND_TOKEN_REJECTED
        else:
            kind = KIND_REJECTED

        # An HTTP rejection means the network is fine and CC said no — the
        # same token will get the same answer, so don't retry.
        logger.error("Registration failed", status=status, detail=detail)
        return RegistrationResult(
            ok=False, error_kind=kind, status_code=status, detail=detail
        )


def register_with_command_center(
    command_center_url: str,
    node_id: str,
    provisioning_token: str,
    room: str | None = None,
) -> dict | None:
    """Compatibility wrapper around :func:`register_node`.

    Returns:
        Dict with node_id and node_key on success, None on failure
    """
    result = register_node(command_center_url, node_id, provisioning_token, room)
    if not result.ok:
        return None
    return {"node_id": result.node_id, "node_key": result.node_key}
