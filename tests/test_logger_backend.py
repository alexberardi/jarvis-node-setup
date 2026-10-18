"""NodeLoggerBackend + the commands' LazyLogger (go-pi-node §5.2 step 2)."""

import logging
from unittest.mock import MagicMock, patch

import pytest
from jarvis_command_sdk import get_logger, logger as sdk_logger

from commands._sdk_logging import LazyLogger
from services.logger_backend import NodeLoggerBackend, init_logger_backend


@pytest.fixture(autouse=True)
def _restore_logger_backend():
    previous = sdk_logger.get_logger_backend()
    yield
    sdk_logger.set_logger_backend(previous)


class TestNodeLoggerBackend:
    def test_returns_jarvis_logger_with_the_name_as_service(self):
        with patch("services.logger_backend.JarvisLogger") as cls:
            log = NodeLoggerBackend().get_logger("cmd.tell_joke")
        cls.assert_called_once_with(service="cmd.tell_joke")
        assert log is cls.return_value

    def test_one_logger_per_name(self):
        with patch("services.logger_backend.JarvisLogger", side_effect=lambda **kw: MagicMock(**kw)) as cls:
            backend = NodeLoggerBackend()
            a = backend.get_logger("jarvis-node")
            b = backend.get_logger("jarvis-node")
            c = backend.get_logger("cmd.tell_joke")
        assert a is b
        assert a is not c
        assert cls.call_count == 2

    def test_init_registers_with_sdk(self):
        sdk_logger.set_logger_backend(None)
        init_logger_backend()
        assert isinstance(sdk_logger.get_logger_backend(), NodeLoggerBackend)


class TestLazyLogger:
    def test_resolves_backend_registered_after_construction(self):
        sdk_logger.set_logger_backend(None)
        log = LazyLogger("jarvis-node")  # module import time: no backend yet

        target = MagicMock()
        backend = MagicMock()
        backend.get_logger.return_value = target
        sdk_logger.set_logger_backend(backend)

        log.warning("send failed", url="https://x", status=500)

        backend.get_logger.assert_called_with("jarvis-node")
        target.warning.assert_called_once_with("send failed", url="https://x", status=500)

    @pytest.mark.parametrize("level", ["debug", "info", "warning", "error", "critical"])
    def test_stdlib_fallback_accepts_kwargs(self, level, caplog):
        sdk_logger.set_logger_backend(None)
        assert isinstance(get_logger("lazy-test"), logging.Logger)

        with caplog.at_level(logging.DEBUG, logger="lazy-test"):
            getattr(LazyLogger("lazy-test"), level)("routine failed", name="good_morning", error="boom")

        assert len(caplog.records) == 1
        record = caplog.records[0]
        assert record.levelname == level.upper()
        assert record.getMessage() == "routine failed | {'name': 'good_morning', 'error': 'boom'}"

    def test_stdlib_fallback_keeps_stdlib_kwargs(self, caplog):
        sdk_logger.set_logger_backend(None)
        try:
            raise ValueError("bad")
        except ValueError:
            with caplog.at_level(logging.ERROR, logger="lazy-test"):
                LazyLogger("lazy-test").error("failed", exc_info=True, item="milk")

        record = caplog.records[0]
        assert record.getMessage() == "failed | {'item': 'milk'}"
        assert record.exc_info is not None

    def test_stdlib_fallback_without_kwargs_is_plain(self, caplog):
        sdk_logger.set_logger_backend(None)
        with caplog.at_level(logging.INFO, logger="lazy-test"):
            LazyLogger("lazy-test").info("hello")
        assert caplog.records[0].getMessage() == "hello"

    def test_command_module_logger_is_lazy(self):
        from commands import routine_command, tell_a_joke_command

        assert isinstance(routine_command.logger, LazyLogger)
        assert routine_command.logger.name == "jarvis-node"
        assert tell_a_joke_command.logger.name == "cmd.tell_joke"
