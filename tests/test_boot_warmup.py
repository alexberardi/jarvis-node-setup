"""Tests for the bounded, text-only boot warmup (scripts.main._run_boot_warmup).

The boot warmup must never block the wake listener. Two regressions previously
left the node wake-dead:
  * the old process_voice_command("hello") streamed a TTS reply through aplay,
    which hangs forever on a wedged ALSA sink (main thread, before the listener);
  * a slow/unreachable command-center or LLM makes the warmup network call hang.
These tests assert the warmup is text-only, bounded (a hang returns within the
timeout), and swallows exceptions.
"""

import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

# Stub C-ext / hardware deps before importing scripts.main (which pulls in
# scripts.voice_listener -> openwakeword, and db.py -> sqlcipher3 at import).
_mock_db = MagicMock()
_mock_db.SessionLocal = MagicMock
_mock_db.engine = MagicMock()
if "sqlcipher3" not in sys.modules:
    sys.modules["sqlcipher3"] = MagicMock()
    sys.modules["sqlcipher3.dbapi2"] = MagicMock()
if "db" not in sys.modules:
    sys.modules["db"] = _mock_db
for _mod in ("openwakeword", "openwakeword.model", "openwakeword.utils"):
    if _mod not in sys.modules:
        sys.modules[_mod] = types.ModuleType(_mod)
sys.modules["openwakeword"].Model = MagicMock()
sys.modules["openwakeword"].utils = MagicMock()
sys.modules["openwakeword.model"].Model = MagicMock()

from scripts.main import _run_boot_warmup

_PATCH_TARGET = "utils.command_execution_service.CommandExecutionService"


def test_warmup_hang_does_not_block_boot():
    """A warmup that hangs must return within the timeout, not forever."""
    release = threading.Event()

    class _HangingService:
        def parse_voice_command(self, *_a, **_k):
            release.wait()  # blocks until the test releases it

    try:
        with patch(_PATCH_TARGET, return_value=_HangingService()):
            t0 = time.monotonic()
            result = _run_boot_warmup(timeout_s=0.3)
            elapsed = time.monotonic() - t0
    finally:
        release.set()  # let the daemon thread exit

    assert result is False, "a hung warmup should report it did not finish"
    assert elapsed < 2.0, f"warmup should be bounded, took {elapsed:.2f}s"


def test_warmup_exception_is_swallowed():
    """A warmup that raises must not propagate; boot continues."""

    class _FailingService:
        def parse_voice_command(self, *_a, **_k):
            raise RuntimeError("llm down")

    with patch(_PATCH_TARGET, return_value=_FailingService()):
        result = _run_boot_warmup(timeout_s=2.0)

    # The thread finished (error caught), so boot proceeds normally.
    assert result is True


def test_warmup_uses_text_only_path_no_audio():
    """The boot warmup must use the text-only parse path, never the audio one."""
    svc = MagicMock()
    with patch(_PATCH_TARGET, return_value=svc):
        result = _run_boot_warmup(timeout_s=2.0)

    assert result is True
    svc.parse_voice_command.assert_called_once_with("hello")
    svc.process_voice_command.assert_not_called()
