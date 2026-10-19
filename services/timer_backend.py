"""Concrete TimerBackend for the node runtime.

Bridges the SDK's ``timer_*`` helpers to the node's TimerService. The service
keeps owning scheduling, the expiry announcement and ``restore_timers``; this
only forwards the command-facing calls. The service is looked up on every call
(it is a singleton), so tests that patch ``get_timer_service`` still apply.

Registered once at startup via:
    from services.timer_backend import init_timer_backend
    init_timer_backend()
"""

from __future__ import annotations

from typing import Any

from jarvis_command_sdk.timer import TimerBackend, set_timer_backend

from services import timer_service


class NodeTimerBackend(TimerBackend):
    """Timer backend backed by the node's in-process TimerService."""

    def set(self, seconds: int, label: str | None = None) -> str | None:
        return timer_service.get_timer_service().set_timer(seconds, label)

    def cancel(self, timer_id: str) -> bool:
        return timer_service.get_timer_service().cancel_timer(timer_id)

    def list_active(self) -> list[dict[str, Any]]:
        return timer_service.get_timer_service().get_active_timers()

    def get(self, timer_id: str) -> dict[str, Any] | None:
        return timer_service.get_timer_service().get_timer(timer_id)

    def find_by_label(self, label: str) -> str | None:
        return timer_service.get_timer_service().find_timer_by_label(label)

    def clear_all(self) -> int:
        return timer_service.get_timer_service().clear_all()


def init_timer_backend() -> None:
    """Register the node timer backend with the SDK. Call once at startup."""
    set_timer_backend(NodeTimerBackend())
