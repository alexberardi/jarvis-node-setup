"""Tests for the aplay stall watchdog in core.platform_abstraction.play_pcm_stream.

A wedged/SUSPENDED ALSA sink stops draining, so a blocking ``proc.stdin.write``
would hang forever (observed live: 88 KB "playing" for 476 s, pinning the thread
that drives playback and starving the wake loop). The watchdog must kill aplay
and bail out within a bounded time — while never firing on healthy playback.
"""

import io
import subprocess
import threading
import time

import core.platform_abstraction as pa
from core.platform_abstraction import AudioProvider


class _StubAudioProvider(AudioProvider):
    def play_audio_file(self, file_path, volume=1.0):
        return True

    def play_chime(self, chime_path):
        return True

    def get_audio_devices(self):
        return []


class _WedgedStdin:
    """stdin whose write() blocks until the proc is killed (a wedged sink)."""

    def __init__(self, killed: threading.Event):
        self._killed = killed
        self.writes = 0

    def write(self, chunk):
        self.writes += 1
        # aplay's pipe is full because the sink stopped draining: block until
        # the watchdog kills aplay, then raise like a real broken pipe.
        self._killed.wait()
        raise BrokenPipeError("aplay killed")

    def close(self):
        pass


class _WedgedProc:
    def __init__(self):
        self._killed = threading.Event()
        self.stdin = _WedgedStdin(self._killed)
        self.stderr = io.BytesIO(b"")
        self.pid = 4242
        self.returncode = None

    def poll(self):
        return -9 if self._killed.is_set() else None

    def kill(self):
        self._killed.set()
        self.returncode = -9

    def wait(self, timeout=None):
        if not self._killed.wait(timeout):
            raise subprocess.TimeoutExpired("aplay", timeout)
        self.returncode = -9
        return -9


class _HealthyStdin:
    def __init__(self):
        self.data = b""

    def write(self, chunk):
        self.data += chunk

    def close(self):
        pass


class _HealthyProc:
    def __init__(self):
        self.stdin = _HealthyStdin()
        self.stderr = io.BytesIO(b"")
        self.pid = 1
        self.returncode = 0
        self.killed = False

    def poll(self):
        return 0

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return 0


def _patch_env(monkeypatch, proc, stall=0.3, poll=0.05):
    monkeypatch.setattr(pa, "_APLAY_WRITE_STALL_TIMEOUT_S", stall)
    monkeypatch.setattr(pa, "_APLAY_WATCHDOG_POLL_S", poll)
    monkeypatch.setattr(pa, "get_output_device", lambda: "default")
    monkeypatch.setattr(pa, "reload_alsa_card_if_suspended", lambda: None)
    monkeypatch.setattr(pa.subprocess, "Popen", lambda *a, **k: proc)


def test_wedged_write_is_killed_within_bounded_time(monkeypatch):
    """A stuck write must be killed by the watchdog, not hang forever."""
    proc = _WedgedProc()
    _patch_env(monkeypatch, proc)

    provider = _StubAudioProvider()
    t0 = time.monotonic()
    result = provider.play_pcm_stream(
        iter([b"\x00\x00" * 100]), sample_rate=24000, channels=1, sample_width=2
    )
    elapsed = time.monotonic() - t0

    assert result is False, "wedged playback should report failure"
    assert proc._killed.is_set(), "watchdog should have killed aplay"
    assert elapsed < 3.0, f"playback should be bounded, took {elapsed:.2f}s"


def test_healthy_playback_is_not_killed(monkeypatch):
    """Normal fast writes must complete without the watchdog firing."""
    proc = _HealthyProc()
    _patch_env(monkeypatch, proc)

    provider = _StubAudioProvider()
    chunks = [b"\x00\x00" * 10 for _ in range(5)]
    result = provider.play_pcm_stream(
        iter(chunks), sample_rate=24000, channels=1, sample_width=2
    )

    assert result is True
    assert proc.killed is False, "watchdog must not kill healthy playback"
    assert proc.stdin.data == b"".join(chunks)
