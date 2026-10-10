"""start_voice_listener must not leak the AudioBus when setup fails.

Regression for the Pi 4 "Memory growth alarm" (RSS +~380 MB/h, +1 thread/min)
on a node whose config-service was unreachable. scripts/main.py retries
``start_voice_listener`` forever on ``ServiceUnresolvedError`` (backoff capped
at 60 s). Each attempt used to load the wake model, open the mic and start an
``AudioBus``, and only THEN construct ``CommandExecutionService`` — which
raised before the ``try/finally`` that stops the bus. Every retry orphaned a
running ``AudioBusProducer`` thread, its own PyAudio instance and an open
capture stream. py-spy on the Pi showed 30+ live ``AudioBusProducer`` threads
all blocked in ``pyaudio.read``.

Contract under test:
  * an unresolvable command-center fails fast, before the wake model loads
    or the mic opens;
  * any failure after the bus started stops it and clears the shared
    ``get_audio_bus()`` handle;
  * N failed retries leave no producer thread behind (real AudioBus, fake
    PyAudio).
"""

from __future__ import annotations

import functools
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest

from core.audio_bus import AudioBus
from utils.service_discovery import ServiceUnresolvedError


def _import_voice_listener():
    """Import scripts.voice_listener with C-ext/hardware deps stubbed
    (same pattern as tests/test_wake_models.py)."""
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
    if not hasattr(sys.modules["openwakeword"], "Model"):
        sys.modules["openwakeword"].Model = MagicMock()
    if not hasattr(sys.modules["openwakeword.model"], "Model"):
        sys.modules["openwakeword.model"].Model = MagicMock()

    import scripts.voice_listener as voice_listener

    return voice_listener


class _FakeStream:
    """Stands in for a PyAudio capture stream: read() blocks briefly like a
    real 80 ms mic read, so a live producer thread stays alive."""

    def __init__(self) -> None:
        self.closed = False

    def read(self, n: int, exception_on_overflow: bool = False) -> bytes:
        time.sleep(0.002)
        return b"\x00\x00" * n

    def stop_stream(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


class _FakePyAudio:
    instances: list["_FakePyAudio"] = []

    def __init__(self) -> None:
        self.terminated = False
        self.streams: list[_FakeStream] = []
        _FakePyAudio.instances.append(self)

    def open(self, **_kw) -> _FakeStream:
        s = _FakeStream()
        self.streams.append(s)
        return s

    def terminate(self) -> None:
        self.terminated = True


def _producer_threads() -> list[threading.Thread]:
    return [
        t for t in threading.enumerate()
        if t.name == "AudioBusProducer" and t.is_alive()
    ]


@pytest.fixture
def vl():
    voice_listener = _import_voice_listener()
    _FakePyAudio.instances = []
    real_bus = functools.partial(
        AudioBus,
        pyaudio_factory=_FakePyAudio,
        device_index_resolver=lambda: None,
    )
    resolved = MagicMock(model_ref="hey_jarvis")
    with patch.object(voice_listener, "AudioBus", side_effect=real_bus), \
            patch.object(voice_listener, "prepare_wake_model", return_value=resolved), \
            patch.object(voice_listener, "OWWModel", MagicMock()), \
            patch.object(voice_listener, "get_stt_provider", MagicMock()), \
            patch.object(voice_listener, "run_warmup", MagicMock()), \
            patch.object(voice_listener, "fetch_next_processing_ack", MagicMock()), \
            patch.object(voice_listener, "run_wake_loop", MagicMock()):
        yield voice_listener
    voice_listener._audio_bus = None


class TestServiceUnresolvedFailsBeforeMic:
    def test_unresolved_command_center_never_opens_mic_or_loads_model(self, vl):
        with patch.object(
            vl, "CommandExecutionService",
            side_effect=ServiceUnresolvedError("config-service down"),
        ), patch.object(vl, "OWWModel") as oww:
            with pytest.raises(ServiceUnresolvedError):
                vl.start_voice_listener(None)

        assert _FakePyAudio.instances == []
        oww.assert_not_called()
        assert vl.get_audio_bus() is None


class TestBusReleasedOnSetupFailure:
    def test_failure_after_bus_start_stops_bus(self, vl):
        with patch.object(vl, "CommandExecutionService", MagicMock()), \
                patch.object(
                    vl, "make_validation_handler",
                    side_effect=ServiceUnresolvedError("down mid-setup"),
                ):
            with pytest.raises(ServiceUnresolvedError):
                vl.start_voice_listener(None)

        assert len(_FakePyAudio.instances) == 1
        pa = _FakePyAudio.instances[0]
        assert pa.terminated
        assert all(s.closed for s in pa.streams)
        assert _producer_threads() == []
        assert vl.get_audio_bus() is None

    def test_clean_exit_stops_bus_and_clears_handle(self, vl):
        with patch.object(vl, "CommandExecutionService", MagicMock()), \
                patch.object(vl, "make_validation_handler", MagicMock()):
            vl.start_voice_listener(None)

        assert _FakePyAudio.instances[0].terminated
        assert _producer_threads() == []
        assert vl.get_audio_bus() is None


class TestRetryLoopStaysBounded:
    """The main.py retry loop calls start_voice_listener once a minute for as
    long as config-service is down. Drive it many times and assert nothing
    accumulates — this is the test that would have caught the Pi 4 leak."""

    RETRIES = 25

    @pytest.mark.parametrize("fail_at", ["command_center", "after_bus_start"])
    def test_repeated_failed_starts_leave_no_threads_or_streams(self, vl, fail_at):
        threads_before = threading.active_count()
        err = ServiceUnresolvedError("config-service down")
        if fail_at == "command_center":
            ces = MagicMock(side_effect=err)
            mvh = MagicMock()
        else:
            ces = MagicMock()
            mvh = MagicMock(side_effect=err)

        with patch.object(vl, "CommandExecutionService", ces), \
                patch.object(vl, "make_validation_handler", mvh):
            for _ in range(self.RETRIES):
                with pytest.raises(ServiceUnresolvedError):
                    vl.start_voice_listener(None)

        assert _producer_threads() == []
        assert threading.active_count() <= threads_before
        open_streams = [
            s for pa in _FakePyAudio.instances for s in pa.streams if not s.closed
        ]
        assert open_streams == []
        assert all(pa.terminated for pa in _FakePyAudio.instances)
