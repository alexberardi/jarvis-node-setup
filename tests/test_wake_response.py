"""Tests for the wake-response module — chime + LED + cached TTS + warmup.

When the wake word fires, this module decides what the user hears and
sees as confirmation, kicks off background warmup so the LLM has a
running start, and pre-generates the NEXT cycle's audio so the next
'Hey Jarvis' plays instantly.

Coverage focus:

  * ``_trim_wav_silence`` — pure WAV-bytes-in/WAV-bytes-out. Hits
    leading + trailing silence, all-silent (no-op), non-16-bit (bail
    early), stereo (collapse across channels for activity detection).

  * ``_bundled_wake_chimes`` — pure glob over a directory. Hits the
    missing-dir / empty-dir / WAV-files-present / mixed-files-present
    branches.

  * ``set_led_transient`` — best-effort import + call; silent on any
    failure (LED hardware may be absent, especially in dev / Docker).

  * ``play_wake_ack`` — three audio paths in priority order:
    cached-LLM (WAV) > bundled chime > live TTS. Plus the failure mode
    where the cached play raises but the file is still cleaned up.

  * ``handle_keyword_detected`` — the wake callback that fires the LED
    transitions, plays the ack (or sleeps when audio is disabled), and
    submits ``fetch_next_wake_response`` to the bg executor.

  * ``fetch_next_wake_response`` — no provider → no-op; provider
    returns text → writes WAKE_FILE + cached audio; provider raises →
    silently swallowed.

  * ``play_processing_ack`` / ``fetch_next_processing_ack`` — pre-cached
    ack play (returns False if no cached file, True if submitted) and
    the TTS pre-generation that produced it.

  * ``run_warmup`` — populates ``result["success"]`` for the calling
    thread; swallows exceptions and reports False on failure.

Module-level state (``_bg_executor`` and ``_wake_paused``) is normally
injected from voice_listener via :func:`set_runtime`. The autouse
fixture installs lightweight stand-ins so each test is hermetic and
fast. Cache-file constants are pointed at ``tmp_path`` per test.
"""

from __future__ import annotations

import io
import wave
from concurrent.futures import Future
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import numpy as np
import pytest

from core import wake_response


# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


class FakeExecutor:
    """Drop-in replacement for ThreadPoolExecutor that runs work
    synchronously so tests can assert on its side effects in-process."""

    def __init__(self) -> None:
        self.submitted: list[tuple] = []

    def submit(self, fn, *args, **kwargs) -> Future:
        self.submitted.append((fn, args, kwargs))
        f: Future = Future()
        try:
            f.set_result(fn(*args, **kwargs))
        except Exception as e:  # pragma: no cover — defensive
            f.set_exception(e)
        return f


@contextmanager
def _spy_paused(spy: list[bool]) -> Iterator[None]:
    """Context manager that records its enter/exit on a shared list."""
    spy.append(True)   # entered
    try:
        yield
    finally:
        spy.append(False)  # exited


@pytest.fixture(autouse=True)
def _isolate_wake_response(monkeypatch, tmp_path):
    """Per-test: redirect cache files to tmp_path, install a fake bg
    executor + no-op wake_paused context manager, scrub any spies."""
    monkeypatch.setattr(wake_response, "WAKE_FILE", tmp_path / "next_wake_response.txt")
    monkeypatch.setattr(wake_response, "WAKE_AUDIO_FILE", tmp_path / "next_wake_response.wav")
    monkeypatch.setattr(wake_response, "PROCESSING_ACK_FILE", tmp_path / "next_processing_ack.wav")
    monkeypatch.setattr(wake_response, "_WAKE_CHIMES_DIR", tmp_path / "sounds_wake")

    fake_exec = FakeExecutor()
    monkeypatch.setattr(wake_response, "_bg_executor", fake_exec)
    monkeypatch.setattr(wake_response, "_wake_paused", lambda: _wake_paused_noop())
    yield fake_exec


@contextmanager
def _wake_paused_noop() -> Iterator[None]:
    yield


def _write_wav(path: Path, samples: np.ndarray, *, rate: int = 16000, nchannels: int = 1) -> bytes:
    """Build a 16-bit WAV from a samples array and write to ``path``.
    Returns the raw bytes so tests can also feed them to ``_trim_wav_silence``."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wav:
        wav.setnchannels(nchannels)
        wav.setsampwidth(2)  # 16-bit
        wav.setframerate(rate)
        wav.writeframes(samples.astype(np.int16).tobytes())
    data = buf.getvalue()
    path.write_bytes(data)
    return data


# ---------------------------------------------------------------------------
# _trim_wav_silence — pure silence trimming for cached TTS responses
# ---------------------------------------------------------------------------


class TestTrimWavSilence:

    def test_trims_leading_and_trailing_silence(self, tmp_path):
        # 100 silent, 200 loud (above threshold 200), 100 silent.
        silent = np.zeros(100, dtype=np.int16)
        loud = np.full(200, 1000, dtype=np.int16)
        samples = np.concatenate([silent, loud, silent])
        data = _write_wav(tmp_path / "s.wav", samples)

        trimmed = wake_response._trim_wav_silence(data)

        with wave.open(io.BytesIO(trimmed), "rb") as wav:
            params = wav.getparams()
            n = wav.getnframes()
        # 5ms pad each side at 16 kHz = 80 frames; original active span
        # is 200 frames; expect ~200 + 2*80 = 360 frames, with the result
        # comfortably smaller than the original 400.
        assert n < 400
        assert n >= 200  # all active samples preserved
        assert params.framerate == 16000
        assert params.sampwidth == 2

    def test_no_silence_returned_intact(self, tmp_path):
        # All samples loud → first/last index don't move → result
        # is the same length (modulo a 5ms pad that gets clamped to bounds).
        loud = np.full(800, 5000, dtype=np.int16)
        data = _write_wav(tmp_path / "s.wav", loud)

        trimmed = wake_response._trim_wav_silence(data)

        with wave.open(io.BytesIO(trimmed), "rb") as wav:
            n = wav.getnframes()
        assert n == 800

    def test_all_silence_returns_original(self, tmp_path):
        # active.any() == False → bail out, return original bytes unchanged.
        silent = np.zeros(500, dtype=np.int16)
        data = _write_wav(tmp_path / "s.wav", silent)

        result = wake_response._trim_wav_silence(data)
        assert result == data

    def test_non_16bit_returns_original(self, tmp_path):
        # 8-bit WAV → sampwidth != 2 → bail out.
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(1)  # 8-bit
            wav.setframerate(16000)
            wav.writeframes(b"\x00" * 1000)
        data = buf.getvalue()

        result = wake_response._trim_wav_silence(data)
        assert result == data

    def test_stereo_collapses_across_channels(self, tmp_path):
        # Stereo: silent on both, loud on both, silent on both → trimmed
        # exactly like mono. Activity is per-frame (any channel above thr).
        silent = np.zeros(100 * 2, dtype=np.int16)
        loud = np.tile(np.array([1000, 0], dtype=np.int16), 200)
        # Each frame has one loud channel — np.any along axis=1 → True.
        samples = np.concatenate([silent, loud, silent])
        data = _write_wav(tmp_path / "s.wav", samples, nchannels=2)

        trimmed = wake_response._trim_wav_silence(data)

        with wave.open(io.BytesIO(trimmed), "rb") as wav:
            n = wav.getnframes()
        # 400 frames in source (per-channel-pair); trimmed should be smaller.
        assert n < 400


# ---------------------------------------------------------------------------
# _bundled_wake_chimes — directory glob
# ---------------------------------------------------------------------------


class TestBundledWakeChimes:

    def test_missing_dir_returns_empty(self):
        # The autouse fixture pointed _WAKE_CHIMES_DIR at a tmp path
        # that doesn't exist yet.
        assert wake_response._bundled_wake_chimes() == []

    def test_empty_dir_returns_empty(self):
        wake_response._WAKE_CHIMES_DIR.mkdir()
        assert wake_response._bundled_wake_chimes() == []

    def test_returns_sorted_wavs(self):
        wake_response._WAKE_CHIMES_DIR.mkdir()
        for name in ["c.wav", "a.wav", "b.wav"]:
            (wake_response._WAKE_CHIMES_DIR / name).write_bytes(b"fake wav")
        result = wake_response._bundled_wake_chimes()
        assert [p.name for p in result] == ["a.wav", "b.wav", "c.wav"]

    def test_only_wavs_returned(self):
        wake_response._WAKE_CHIMES_DIR.mkdir()
        (wake_response._WAKE_CHIMES_DIR / "ok.wav").write_bytes(b"fake")
        (wake_response._WAKE_CHIMES_DIR / "ignore.mp3").write_bytes(b"fake")
        (wake_response._WAKE_CHIMES_DIR / "ignore.txt").write_bytes(b"fake")
        result = wake_response._bundled_wake_chimes()
        assert [p.name for p in result] == ["ok.wav"]


# ---------------------------------------------------------------------------
# set_led_transient — best-effort; silent on every failure path
# ---------------------------------------------------------------------------


class TestSetLedTransient:

    def test_passes_pattern_to_led_service(self, monkeypatch):
        fake_service = MagicMock()
        fake_get_service = MagicMock(return_value=fake_service)
        # The import happens inside the function — monkeypatch the
        # module attribute as it's resolved by `services.led_service`.
        import sys
        import types
        fake_module = types.ModuleType("services.led_service")
        fake_module.get_led_service = fake_get_service  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "services.led_service", fake_module)

        wake_response.set_led_transient("wake_detected")

        fake_service.set_transient_pattern.assert_called_once_with("wake_detected")

    def test_silent_on_import_error(self, monkeypatch):
        # No services.led_service installed; the import inside the
        # function will raise — must be swallowed.
        import sys
        monkeypatch.setitem(sys.modules, "services.led_service", None)
        # None as the module value makes `from services.led_service import ...`
        # raise ImportError on attribute lookup; the function must be silent.
        wake_response.set_led_transient("wake_detected")

    def test_silent_on_service_failure(self, monkeypatch):
        import sys
        import types
        fake_module = types.ModuleType("services.led_service")

        def boom():
            raise RuntimeError("LED hardware missing")
        fake_module.get_led_service = boom  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "services.led_service", fake_module)

        # Must not raise.
        wake_response.set_led_transient("listening")


# ---------------------------------------------------------------------------
# play_wake_ack — three-tier fallback: cached LLM > bundled chime > TTS
# ---------------------------------------------------------------------------


class TestPlayWakeAck:

    def test_plays_cached_llm_audio_when_present(self, monkeypatch):
        # WAKE_AUDIO_FILE on disk → plays it, unlinks both wake files.
        wake_response.WAKE_AUDIO_FILE.write_bytes(b"cached wav bytes")
        wake_response.WAKE_FILE.write_text("Yes?")
        play_calls: list[str] = []
        monkeypatch.setattr(
            wake_response.platform_audio, "play_audio_file",
            lambda path, **kw: play_calls.append(path) or True,
        )

        wake_response.play_wake_ack()

        assert len(play_calls) == 1
        assert play_calls[0] == str(wake_response.WAKE_AUDIO_FILE)
        # Both cache files cleaned up after a successful play.
        assert not wake_response.WAKE_AUDIO_FILE.exists()
        assert not wake_response.WAKE_FILE.exists()

    def test_cached_play_runs_inside_wake_paused(self, monkeypatch):
        # The wake-word model must be paused while we play our own
        # response audio, or it re-fires on what it hears.
        wake_response.WAKE_AUDIO_FILE.write_bytes(b"cached wav bytes")
        spy: list = []
        monkeypatch.setattr(wake_response, "_wake_paused", lambda: _spy_paused(spy))
        monkeypatch.setattr(
            wake_response.platform_audio, "play_audio_file",
            lambda path, **kw: spy.append("played") or True,
        )

        wake_response.play_wake_ack()

        # Entered (True) -> played -> exited (False) — strict order.
        assert spy == [True, "played", False]

    def test_falls_back_to_bundled_chime(self, monkeypatch):
        # No cached LLM audio, but a bundled chime exists.
        wake_response._WAKE_CHIMES_DIR.mkdir()
        chime = wake_response._WAKE_CHIMES_DIR / "chime.wav"
        chime.write_bytes(b"chime")
        played: list[str] = []
        monkeypatch.setattr(
            wake_response.platform_audio, "play_audio_file",
            lambda path, **kw: played.append(path) or True,
        )

        wake_response.play_wake_ack()

        assert played == [str(chime)]

    def test_falls_back_to_tts_when_no_audio(self, monkeypatch):
        # No cached audio, no bundled chimes — speaks "Yes?" via TTS.
        spoken: list[tuple] = []
        fake_tts = MagicMock()
        fake_tts.speak = lambda include_chime, text: spoken.append((include_chime, text))
        monkeypatch.setattr(wake_response, "get_tts_provider", lambda: fake_tts)
        monkeypatch.setattr(
            wake_response.platform_audio, "play_audio_file",
            lambda *a, **kw: False,
        )

        wake_response.play_wake_ack()

        assert spoken == [(False, "Yes?")]

    def test_tts_reads_wake_file_when_present(self, monkeypatch):
        # No cached audio, no chimes, but WAKE_FILE has pre-fetched text.
        wake_response.WAKE_FILE.write_text("Hi there.")
        spoken: list[tuple] = []
        fake_tts = MagicMock()
        fake_tts.speak = lambda include_chime, text: spoken.append((include_chime, text))
        monkeypatch.setattr(wake_response, "get_tts_provider", lambda: fake_tts)
        monkeypatch.setattr(
            wake_response.platform_audio, "play_audio_file",
            lambda *a, **kw: False,
        )

        wake_response.play_wake_ack()

        assert spoken == [(False, "Hi there.")]
        # WAKE_FILE consumed — won't be re-spoken next call.
        assert not wake_response.WAKE_FILE.exists()

    def test_cached_play_exception_still_cleans_files(self, monkeypatch):
        # platform_audio.play_audio_file raises; the finally branch
        # must still unlink the cache files.
        wake_response.WAKE_AUDIO_FILE.write_bytes(b"cached")
        wake_response.WAKE_FILE.write_text("Yes?")

        def boom(*a, **kw):
            raise RuntimeError("playback hardware gone")
        monkeypatch.setattr(wake_response.platform_audio, "play_audio_file", boom)
        # TTS fallback also gets called since cached path didn't set
        # played=True; tolerate a TTS provider being called.
        fake_tts = MagicMock()
        fake_tts.speak = MagicMock()
        monkeypatch.setattr(wake_response, "get_tts_provider", lambda: fake_tts)

        # Must not raise.
        wake_response.play_wake_ack()

        # Cache files cleaned up despite the failure.
        assert not wake_response.WAKE_AUDIO_FILE.exists()
        assert not wake_response.WAKE_FILE.exists()


# ---------------------------------------------------------------------------
# handle_keyword_detected — wake-fire callback orchestrator
# ---------------------------------------------------------------------------


class TestHandleKeywordDetected:

    def test_full_path_with_audio_enabled(self, monkeypatch, _isolate_wake_response):
        # Audio enabled: LED purple, play_wake_ack, LED blue, submit
        # fetch_next_wake_response to the bg executor.
        led_calls: list[str | None] = []
        monkeypatch.setattr(
            wake_response, "set_led_transient",
            lambda p: led_calls.append(p),
        )
        play_calls: list[bool] = []
        monkeypatch.setattr(
            wake_response, "play_wake_ack",
            lambda: play_calls.append(True),
        )
        monkeypatch.setattr(
            wake_response.Config, "get_bool",
            lambda key, default: True,
        )

        wake_response.handle_keyword_detected()

        # Both LED transitions fired, in order.
        assert led_calls == ["wake_detected", "listening"]
        assert play_calls == [True]
        # fetch_next_wake_response was submitted.
        fns = [s[0] for s in _isolate_wake_response.submitted]
        assert wake_response.fetch_next_wake_response in fns

    def test_audio_disabled_sleeps_then_transitions(self, monkeypatch, _isolate_wake_response):
        led_calls: list[str | None] = []
        monkeypatch.setattr(
            wake_response, "set_led_transient",
            lambda p: led_calls.append(p),
        )
        sleep_calls: list[float] = []
        monkeypatch.setattr(wake_response.time, "sleep", lambda s: sleep_calls.append(s))
        ack_calls: list[bool] = []
        monkeypatch.setattr(
            wake_response, "play_wake_ack",
            lambda: ack_calls.append(True),
        )
        monkeypatch.setattr(
            wake_response.Config, "get_bool",
            lambda key, default: False,
        )

        wake_response.handle_keyword_detected()

        # Audio path NOT taken — sleep covers the LED visibility window.
        assert ack_calls == []
        assert sleep_calls == [0.2]
        assert led_calls == ["wake_detected", "listening"]


# ---------------------------------------------------------------------------
# fetch_next_wake_response — pre-generate the next wake ack
# ---------------------------------------------------------------------------


class TestFetchNextWakeResponse:

    def test_no_provider_is_noop(self, monkeypatch):
        monkeypatch.setattr(wake_response, "get_wake_response_provider", lambda: None)
        # No provider → nothing should hit disk.
        wake_response.fetch_next_wake_response()
        assert not wake_response.WAKE_FILE.exists()
        assert not wake_response.WAKE_AUDIO_FILE.exists()

    def test_provider_returns_empty_is_noop(self, monkeypatch):
        fake_provider = MagicMock()
        fake_provider.fetch_next_wake_response = lambda: ""
        monkeypatch.setattr(
            wake_response, "get_wake_response_provider", lambda: fake_provider
        )
        wake_response.fetch_next_wake_response()
        assert not wake_response.WAKE_FILE.exists()
        assert not wake_response.WAKE_AUDIO_FILE.exists()

    def test_writes_text_and_audio(self, monkeypatch):
        fake_provider = MagicMock()
        fake_provider.fetch_next_wake_response = lambda: "Sure thing."
        monkeypatch.setattr(
            wake_response, "get_wake_response_provider", lambda: fake_provider
        )
        monkeypatch.setattr(
            wake_response, "get_command_center_url", lambda: "http://cc:7703"
        )
        # Return a tiny WAV so the silence-trim path is exercised but
        # doesn't choke; non-WAV bytes would crash _trim_wav_silence
        # which is wrapped in a try/except — both branches OK.
        loud = np.full(800, 5000, dtype=np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(16000)
            wav.writeframes(loud.tobytes())
        audio_bytes = buf.getvalue()
        monkeypatch.setattr(
            wake_response.RestClient, "post_binary",
            lambda url, data, timeout: audio_bytes,
        )

        wake_response.fetch_next_wake_response()

        assert wake_response.WAKE_FILE.read_text() == "Sure thing."
        assert wake_response.WAKE_AUDIO_FILE.exists()
        assert wake_response.WAKE_AUDIO_FILE.stat().st_size > 0

    def test_writes_text_only_when_no_cc_url(self, monkeypatch):
        fake_provider = MagicMock()
        fake_provider.fetch_next_wake_response = lambda: "Sure thing."
        monkeypatch.setattr(
            wake_response, "get_wake_response_provider", lambda: fake_provider
        )
        monkeypatch.setattr(wake_response, "get_command_center_url", lambda: "")
        wake_response.fetch_next_wake_response()

        assert wake_response.WAKE_FILE.read_text() == "Sure thing."
        assert not wake_response.WAKE_AUDIO_FILE.exists()

    def test_provider_exception_swallowed(self, monkeypatch):
        def boom():
            raise RuntimeError("provider broke")
        fake_provider = MagicMock()
        fake_provider.fetch_next_wake_response = boom
        monkeypatch.setattr(
            wake_response, "get_wake_response_provider", lambda: fake_provider
        )
        # Must not raise.
        wake_response.fetch_next_wake_response()

    def test_silence_trim_failure_falls_back_to_original(self, monkeypatch):
        fake_provider = MagicMock()
        fake_provider.fetch_next_wake_response = lambda: "Sure thing."
        monkeypatch.setattr(
            wake_response, "get_wake_response_provider", lambda: fake_provider
        )
        monkeypatch.setattr(
            wake_response, "get_command_center_url", lambda: "http://cc:7703"
        )
        monkeypatch.setattr(
            wake_response.RestClient, "post_binary",
            lambda url, data, timeout: b"not a real wav",
        )

        def boom(_):
            raise ValueError("not WAV")
        monkeypatch.setattr(wake_response, "_trim_wav_silence", boom)

        # Must not raise — fallback writes the un-trimmed bytes.
        wake_response.fetch_next_wake_response()

        assert wake_response.WAKE_AUDIO_FILE.read_bytes() == b"not a real wav"


# ---------------------------------------------------------------------------
# play_processing_ack / fetch_next_processing_ack — short-ack cache
# ---------------------------------------------------------------------------


class TestPlayProcessingAck:

    def test_missing_file_returns_false(self, _isolate_wake_response):
        # No cached ack → False, no submission.
        assert wake_response.play_processing_ack() is False
        assert _isolate_wake_response.submitted == []

    def test_present_file_submits_and_returns_true(self, monkeypatch, _isolate_wake_response):
        wake_response.PROCESSING_ACK_FILE.write_bytes(b"ack wav")
        play_calls: list[str] = []
        monkeypatch.setattr(
            wake_response.platform_audio, "play_audio_file",
            lambda path, **kw: play_calls.append(path) or True,
        )

        assert wake_response.play_processing_ack() is True
        # The submitted closure ran via FakeExecutor; the file was
        # played and then unlinked.
        assert play_calls == [str(wake_response.PROCESSING_ACK_FILE)]
        assert not wake_response.PROCESSING_ACK_FILE.exists()

    def test_play_exception_still_unlinks(self, monkeypatch, _isolate_wake_response):
        wake_response.PROCESSING_ACK_FILE.write_bytes(b"ack wav")

        def boom(*a, **kw):
            raise RuntimeError("audio gone")
        monkeypatch.setattr(wake_response.platform_audio, "play_audio_file", boom)

        assert wake_response.play_processing_ack() is True
        # Despite the play failure, the file must be removed so we
        # don't replay a stale ack next cycle.
        assert not wake_response.PROCESSING_ACK_FILE.exists()


class TestFetchNextProcessingAck:

    def test_no_cc_url_is_noop(self, monkeypatch):
        monkeypatch.setattr(wake_response, "get_command_center_url", lambda: "")
        wake_response.fetch_next_processing_ack()
        assert not wake_response.PROCESSING_ACK_FILE.exists()

    def test_caches_audio_from_tts(self, monkeypatch):
        monkeypatch.setattr(
            wake_response, "get_command_center_url", lambda: "http://cc:7703"
        )
        posted: list[dict] = []

        def fake_post(url, data, timeout):
            posted.append(data)
            return b"ack wav bytes"
        monkeypatch.setattr(wake_response.RestClient, "post_binary", fake_post)

        wake_response.fetch_next_processing_ack()

        assert wake_response.PROCESSING_ACK_FILE.read_bytes() == b"ack wav bytes"
        # The text came from the constant pool.
        assert posted[0]["text"] in wake_response._PROCESSING_ACK_POOL

    def test_exception_swallowed(self, monkeypatch):
        monkeypatch.setattr(
            wake_response, "get_command_center_url", lambda: "http://cc:7703"
        )

        def boom(*a, **kw):
            raise RuntimeError("tts down")
        monkeypatch.setattr(wake_response.RestClient, "post_binary", boom)

        # Must not raise — failure is non-fatal.
        wake_response.fetch_next_processing_ack()
        assert not wake_response.PROCESSING_ACK_FILE.exists()

    def test_empty_audio_response_does_not_write(self, monkeypatch):
        monkeypatch.setattr(
            wake_response, "get_command_center_url", lambda: "http://cc:7703"
        )
        monkeypatch.setattr(
            wake_response.RestClient, "post_binary",
            lambda url, data, timeout: None,
        )
        wake_response.fetch_next_processing_ack()
        assert not wake_response.PROCESSING_ACK_FILE.exists()


# ---------------------------------------------------------------------------
# run_warmup — populates result["success"] from a worker thread
# ---------------------------------------------------------------------------


class TestRunWarmup:

    def test_success_populates_result(self):
        result: dict = {}
        command_service = MagicMock()
        command_service.register_tools_for_conversation.return_value = True

        wake_response.run_warmup(
            command_service,
            conversation_id="conv-1",
            speaker_user_id=42,
            speaker_confidence=0.9,
            result=result,
        )

        assert result["success"] is True
        command_service.register_tools_for_conversation.assert_called_once_with(
            "conv-1",
            speaker_user_id=42,
            speaker_confidence=0.9,
        )

    def test_register_returns_false(self):
        result: dict = {}
        command_service = MagicMock()
        command_service.register_tools_for_conversation.return_value = False
        wake_response.run_warmup(
            command_service,
            conversation_id="c",
            speaker_user_id=None,
            speaker_confidence=None,
            result=result,
        )
        assert result["success"] is False

    def test_exception_marks_failure(self):
        result: dict = {}
        command_service = MagicMock()
        command_service.register_tools_for_conversation.side_effect = RuntimeError("warmup broke")
        wake_response.run_warmup(
            command_service,
            conversation_id="c",
            speaker_user_id=None,
            speaker_confidence=None,
            result=result,
        )
        # Failures populate False rather than re-raise — the caller
        # joins the thread and checks the result.
        assert result["success"] is False


# ---------------------------------------------------------------------------
# set_runtime — DI hook used by voice_listener at module init
# ---------------------------------------------------------------------------


class TestSetRuntime:

    def test_installs_executor_and_wake_paused_factory(self, monkeypatch):
        sentinel_exec = MagicMock(name="executor")
        sentinel_cm = MagicMock(name="wake_paused_factory")
        wake_response.set_runtime(
            bg_executor=sentinel_exec,
            wake_paused_factory=sentinel_cm,
        )
        assert wake_response._bg_executor is sentinel_exec
        assert wake_response._wake_paused is sentinel_cm
