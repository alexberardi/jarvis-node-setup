"""Concrete InboxBackend for the node runtime.

Bridges the SDK's JarvisInbox facade to command-center's node inbox
endpoint (``POST {cc}/api/v0/node/inbox-item``). CC resolves household_id
from the authenticated node and injects ``metadata.node_id`` server-side —
never set it here.

Registered once at startup via:
    from services.inbox_backend import init_inbox_backend
    init_inbox_backend()
"""

from __future__ import annotations

from typing import Any

from jarvis_command_sdk.inbox import InboxBackend, set_inbox_backend

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


class NodeInboxBackend(InboxBackend):
    """Inbox backend that POSTs to command-center on behalf of the node.

    Returns discriminated string tags (never raises) so the facade's
    callers can map each failure mode to a distinct voice response —
    same lesson as the export-shopping-list command's _post_inbox_item:
        "ok"           — the item was posted
        "invalid"      — title missing/blank (CC silently drops those)
        "import_error" — clients.rest_client / utils.service_discovery missing
        "no_cc_url"    — service discovery returned no command-center URL
        "http_error"   — the POST failed (timeout, non-2xx, unreachable)
    """

    def post_inbox_item(
        self,
        command_name: str,
        *,
        title: str,
        summary: str = "",
        body: str = "",
        category: str = "general",
        metadata: dict[str, Any] | None = None,
        user_id: int | None = None,
        create_push_notification: bool = False,
        target_type: str = "household",
    ) -> str:
        if not (title or "").strip():
            logger.error(
                "inbox post rejected: empty title", command=command_name
            )
            return "invalid"

        # Deferred imports so a broken runtime degrades to a tag instead
        # of an exception propagating into a community package.
        try:
            from clients.rest_client import RestClient
            from utils.service_discovery import get_command_center_url
        except ImportError as e:
            logger.error(
                "inbox post import failed", command=command_name, error=str(e)
            )
            return "import_error"

        cc_url = get_command_center_url()
        if not cc_url:
            logger.error(
                "inbox post: get_command_center_url returned empty",
                command=command_name,
            )
            return "no_cc_url"

        payload: dict[str, Any] = {
            "title": title,
            "summary": summary,
            "body": body,
            "category": category,
            "metadata": metadata,
            "user_id": user_id,
            "create_push_notification": create_push_notification,
            "target_type": target_type,
        }
        result = RestClient.post(
            f"{cc_url}/api/v0/node/inbox-item",
            data=payload,
            timeout=5,
        )
        if result is None:
            return "http_error"
        if not result.get("sent"):
            # CC accepts the POST but reports the item wasn't delivered
            # (e.g. node has no household, or notifications is down) —
            # a 200 with sent=false is still a failed post.
            logger.error(
                "Inbox post accepted but not delivered (sent=false)",
                command=command_name,
            )
            return "http_error"
        return "ok"


def init_inbox_backend() -> None:
    """Register the node inbox backend with the SDK. Call once at startup."""
    set_inbox_backend(NodeInboxBackend())
