"""Concrete AlertQueueBackend for the node runtime.

Bridges the SDK's ``alerts_*`` helpers to the node's AlertQueueService (which
keeps owning dedupe, expiry, persistence and the LED/on_change hooks). The
queue holds ``core.alert.Alert``, which is the SDK ``Alert``, so nothing is
converted. The service is looked up on every call, so tests that patch
``get_alert_queue_service`` still apply.

Registered once at startup via:
    from services.alert_queue_backend import init_alert_queue_backend
    init_alert_queue_backend()
"""

from __future__ import annotations

from jarvis_command_sdk.agent import Alert
from jarvis_command_sdk.alert_queue import AlertQueueBackend, set_alert_queue_backend

from services import alert_queue_service


class NodeAlertQueueBackend(AlertQueueBackend):
    """Alert-queue backend backed by the node's AlertQueueService."""

    def pending(self) -> list[Alert]:
        return alert_queue_service.get_alert_queue_service().get_pending()

    def flush(self) -> list[Alert]:
        return alert_queue_service.get_alert_queue_service().flush()

    def remove(self, ids: list[str]) -> int:
        return alert_queue_service.get_alert_queue_service().remove_ids(ids)

    def count(self) -> int:
        return alert_queue_service.get_alert_queue_service().count()


def init_alert_queue_backend() -> None:
    """Register the node alert-queue backend with the SDK. Call once at startup."""
    set_alert_queue_backend(NodeAlertQueueBackend())
