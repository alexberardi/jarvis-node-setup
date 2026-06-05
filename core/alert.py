"""Re-export of the unified ``Alert`` dataclass from ``jarvis_command_sdk``.

Historical note: this module used to define its own ``Alert`` dataclass with
``created_at`` / ``expires_at`` / ``is_expired`` while the SDK Alert had only
the four core fields. Agents imported from one or the other inconsistently,
so SDK Alerts reached the queue without ``is_expired`` and crashed it (logged
as ``'Alert' object has no attribute 'is_expired'``). The SDK Alert now owns
those fields with sensible defaults; this shim keeps existing
``from core.alert import Alert`` callers working.
"""

from jarvis_command_sdk import Alert

__all__ = ["Alert"]
