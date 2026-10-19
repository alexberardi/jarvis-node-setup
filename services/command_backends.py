"""Registers the SDK backends that built-in commands reach node services through.

Timer, audio control, alert queue, command-center HTTP and LLM (go-pi-node
§5.2): the commands call the SDK helpers (``timer_set``, ``audio_set_muted``,
``alerts_flush``, ``cc_post``, ``llm_chat_text``), and these backends bridge
them to the node's own services. Each one is registered on its own, so one
failing to import leaves the others working (its helpers then return their
safe defaults).

Called once at startup (scripts/main.py, scripts/text_mode.py):
    from services.command_backends import init_command_backends
    init_command_backends()
"""

from __future__ import annotations

from typing import Callable

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


def _timer() -> None:
    from services.timer_backend import init_timer_backend
    init_timer_backend()


def _audio_control() -> None:
    from services.audio_control_backend import init_audio_control_backend
    init_audio_control_backend()


def _alert_queue() -> None:
    from services.alert_queue_backend import init_alert_queue_backend
    init_alert_queue_backend()


def _cc() -> None:
    from services.cc_backend import init_cc_backend
    init_cc_backend()


def _llm() -> None:
    from services.cc_backend import init_llm_backend
    init_llm_backend()


_BACKENDS: list[tuple[str, Callable[[], None]]] = [
    ("timer", _timer),
    ("audio_control", _audio_control),
    ("alert_queue", _alert_queue),
    ("cc_http", _cc),
    ("llm", _llm),
]


def init_command_backends() -> list[str]:
    """Register every command backend; return the names that failed."""
    failed: list[str] = []
    for name, init in _BACKENDS:
        try:
            init()
        except Exception as e:
            failed.append(name)
            logger.warning(
                "SDK backend init failed, its helpers return defaults",
                backend=name, error=str(e),
            )
    return failed
