"""Concrete LoggerBackend for the node runtime.

Bridges the SDK's ``get_logger(name)`` to ``jarvis_log_client.JarvisLogger``
so command logs keep reaching jarvis-logs. ``name`` is the JarvisLogger
service ("jarvis-node" for most built-in commands, "cmd.tell_joke" for
tell_a_joke), one cached instance per name.

Registered once at startup via:
    from services.logger_backend import init_logger_backend
    init_logger_backend()
"""

from __future__ import annotations

import threading
from typing import Any

from jarvis_command_sdk.logger import LoggerBackend, set_logger_backend

from jarvis_log_client import JarvisLogger


class NodeLoggerBackend(LoggerBackend):
    """Logger backend returning one ``JarvisLogger(service=name)`` per name."""

    def __init__(self) -> None:
        self._loggers: dict[str, Any] = {}
        self._lock = threading.Lock()

    def get_logger(self, name: str) -> Any:
        with self._lock:
            logger = self._loggers.get(name)
            if logger is None:
                logger = JarvisLogger(service=name)
                self._loggers[name] = logger
            return logger


def init_logger_backend() -> None:
    """Register the node logger backend with the SDK. Call once at startup."""
    set_logger_backend(NodeLoggerBackend())
