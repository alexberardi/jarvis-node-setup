"""Lazy, kwargs-tolerant logger for the built-in commands (SDK only).

Commands log with structured kwargs (``logger.warning("msg", url=u)``), which
``jarvis_log_client.JarvisLogger`` accepts but the stdlib logger the SDK's
``get_logger`` falls back to does not. And a module-level
``get_logger(...)`` runs at import, before the node registers its
``LoggerBackend``, so it would stick to stdlib for good.

``LazyLogger`` resolves ``get_logger(name)`` on every call: on the node that
is the registered backend's logger (``services.logger_backend``, the same
``JarvisLogger`` the commands built themselves before); elsewhere (tests,
the Go node's sidecar, Pantry validation) it is stdlib, with the kwargs
folded into the message.
"""

from __future__ import annotations

import logging
from typing import Any

from jarvis_command_sdk import get_logger

_STDLIB_KWARGS = ("exc_info", "stack_info", "stacklevel", "extra")


class LazyLogger:
    """A logger proxy that looks ``get_logger(name)`` up at call time."""

    def __init__(self, name: str) -> None:
        self.name = name

    def _log(self, level: str, message: str, *args: Any, **context: Any) -> None:
        target = get_logger(self.name)
        if not isinstance(target, logging.Logger):
            getattr(target, level)(message, *args, **context)
            return
        std = {k: context.pop(k) for k in _STDLIB_KWARGS if k in context}
        if context:
            message = f"{message} | {context}"
        getattr(target, level)(message, *args, **std)

    def debug(self, message: str, *args: Any, **context: Any) -> None:
        self._log("debug", message, *args, **context)

    def info(self, message: str, *args: Any, **context: Any) -> None:
        self._log("info", message, *args, **context)

    def warning(self, message: str, *args: Any, **context: Any) -> None:
        self._log("warning", message, *args, **context)

    def error(self, message: str, *args: Any, **context: Any) -> None:
        self._log("error", message, *args, **context)

    def critical(self, message: str, *args: Any, **context: Any) -> None:
        self._log("critical", message, *args, **context)
