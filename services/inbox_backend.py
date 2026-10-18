"""Concrete InboxBackend for the node runtime.

Bridges the SDK's JarvisInbox facade to command-center's node inbox
endpoint (``POST {cc}/api/v0/node/inbox-item``). Attachments are uploaded
first, one raw-body ``POST {cc}/api/v0/node/inbox-attachment`` each, and the
item then references them by ``attachment_ids`` (see
docs/decisions/2026-10-18-inbox-attachments-node.md). CC resolves household_id
from the authenticated node and injects ``metadata.node_id`` server-side —
never set it here.

Registered once at startup via:
    from services.inbox_backend import init_inbox_backend
    init_inbox_backend()
"""

from __future__ import annotations

import time
from typing import Any

from jarvis_command_sdk.inbox import Attachment, InboxBackend, set_inbox_backend

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

# A 25 MB video from a Pi over Wi-Fi needs far longer than the item POST's 5 s.
_UPLOAD_TIMEOUT = (5, 60)  # (connect, read) seconds
_UPLOAD_ATTEMPTS = 3
_UPLOAD_BACKOFF_S = 0.5  # doubled after each failed attempt

# jarvisd status -> tag, for rejections of an upload or of the item.
_REJECTION_TAGS = {413: "attachment_too_large", 415: "attachment_type"}


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
    With attachments, also:
        "attachment_too_large" — jarvisd answered 413 (file or item total)
        "attachment_type"      — jarvisd answered 415 (type not allowed/mislabelled)
        "invalid"              — also: an attachment file is missing, or
                                 jarvisd rejected the attachment_ids (422)
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
        attachments: list[Attachment] | None = None,
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
        if attachments:
            return self._post_with_attachments(
                RestClient, cc_url, command_name, payload, attachments
            )

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

    def _post_with_attachments(
        self,
        rest_client: Any,
        cc_url: str,
        command_name: str,
        payload: dict[str, Any],
        attachments: list[Attachment],
    ) -> str:
        """Upload each attachment, then post the item referencing their ids.

        Uses the shared session directly rather than RestClient.post, which
        JSON-encodes the body and hides the status code we map to tags.
        """
        import requests

        ids: list[str] = []
        for att in attachments:
            tag_or_id = self._upload(rest_client, cc_url, command_name, att)
            if not tag_or_id.startswith("id:"):
                return tag_or_id
            ids.append(tag_or_id[3:])

        try:
            resp = rest_client._get_session().post(
                f"{cc_url}/api/v0/node/inbox-item",
                json={**payload, "attachment_ids": ids},
                headers=rest_client._build_auth_header(),
                timeout=5,
            )
        except requests.RequestException as e:
            logger.error(
                "inbox post with attachments failed",
                command=command_name, error=str(e),
            )
            return "http_error"
        if resp.status_code >= 400:
            logger.error(
                "inbox item with attachments rejected",
                command=command_name,
                status=resp.status_code,
                attachment_ids=ids,
                reason=resp.text[:500],
            )
            if resp.status_code == 422:
                return "invalid"
            return _REJECTION_TAGS.get(resp.status_code, "http_error")
        try:
            sent = bool(resp.json().get("sent"))
        except ValueError:
            sent = False
        if not sent:
            logger.error(
                "Inbox post accepted but not delivered (sent=false)",
                command=command_name,
            )
            return "http_error"
        return "ok"

    def _upload(
        self,
        rest_client: Any,
        cc_url: str,
        command_name: str,
        att: Attachment,
    ) -> str:
        """Upload one attachment. Returns ``"id:<id>"`` or a failure tag.

        A path is streamed from disk (requests sends a file object in chunks),
        re-opened on every attempt so a retry starts from byte 0. Connection
        errors and 5xx other than 503 are retried; 503 (server has no
        attachments) and every 4xx are final.
        """
        import requests

        try:
            size = att.size
        except OSError as e:
            logger.error(
                "inbox attachment unreadable",
                command=command_name, filename=att.filename, error=str(e),
            )
            return "invalid"

        url = f"{cc_url}/api/v0/node/inbox-attachment"
        headers = {
            **rest_client._build_auth_header(),
            "Content-Type": att.mime,
            "Content-Length": str(size),
        }
        backoff = _UPLOAD_BACKOFF_S
        for attempt in range(1, _UPLOAD_ATTEMPTS + 1):
            try:
                if att.path is not None:
                    with open(att.path, "rb") as f:
                        resp = rest_client._get_session().post(
                            url, data=f, headers=headers, timeout=_UPLOAD_TIMEOUT
                        )
                else:
                    resp = rest_client._get_session().post(
                        url, data=att.data, headers=headers, timeout=_UPLOAD_TIMEOUT
                    )
            except OSError as e:
                # requests.ConnectionError is an OSError too; a file that
                # vanished between stat and open is not worth retrying.
                if not isinstance(e, requests.ConnectionError):
                    logger.error(
                        "inbox attachment unreadable",
                        command=command_name, filename=att.filename, error=str(e),
                    )
                    return "invalid"
                logger.warning(
                    "inbox attachment upload failed",
                    command=command_name, attempt=attempt, error=str(e),
                )
            except requests.RequestException as e:
                logger.error(
                    "inbox attachment upload failed",
                    command=command_name, attempt=attempt, error=str(e),
                )
                return "http_error"
            else:
                status = resp.status_code
                if 200 <= status < 300:
                    try:
                        return f"id:{resp.json()['id']}"
                    except (ValueError, KeyError, TypeError):
                        logger.error(
                            "inbox attachment: no id in response",
                            command=command_name, status=status,
                        )
                        return "http_error"
                if status == 503:
                    logger.error(
                        "inbox attachment refused: server has no attachments",
                        command=command_name, status=status,
                        reason=resp.text[:500],
                    )
                    return "http_error"
                if status < 500:
                    logger.error(
                        "inbox attachment rejected",
                        command=command_name,
                        status=status,
                        mime=att.mime,
                        size=size,
                        filename=att.filename,
                        reason=resp.text[:500],
                    )
                    return _REJECTION_TAGS.get(status, "http_error")
                logger.warning(
                    "inbox attachment upload failed",
                    command=command_name, attempt=attempt, status=status,
                )
            if attempt < _UPLOAD_ATTEMPTS:
                time.sleep(backoff)
                backoff *= 2
        logger.error(
            "inbox attachment upload gave up",
            command=command_name, attempts=_UPLOAD_ATTEMPTS,
            mime=att.mime, size=size, filename=att.filename,
        )
        return "http_error"


def init_inbox_backend() -> None:
    """Register the node inbox backend with the SDK. Call once at startup."""
    set_inbox_backend(NodeInboxBackend())
