"""Tests for music-state detection + ducking orchestration.

Every external command (pactl, pkill) is mocked. We assert on the
sequence and arguments passed to ``subprocess.run`` rather than running
real PulseAudio, so the suite is hermetic and fast.

Coverage focus:

  * ``is_playing`` — read JSON from pactl, check basename in known set,
    require uncorked. Failure paths (timeout / missing binary / non-zero
    rc / invalid JSON) all return False so we never raise the threshold
    unnecessarily.
  * ``player_sink_input_ids`` / ``player_sink_input_ids_for`` — same
    pactl + basename match shape, different filter sets.
  * ``ensure_duck_null_sink`` — idempotent module load (skips when sink
    already present).
  * ``pause_active_playback`` / ``resume_active_playback`` — verify the
    full sequence of calls and that errors in one step don't prevent
    the others.
"""

import json
import subprocess
from unittest.mock import MagicMock

import pytest

from core import music_control


def _proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    """Build a CompletedProcess-shaped mock."""
    mock = MagicMock()
    mock.stdout = stdout
    mock.returncode = returncode
    return mock


def _sink_inputs(*entries: dict) -> str:
    """Serialise as the JSON pactl produces."""
    return json.dumps(list(entries))


def _entry(index: int, binary: str, corked: bool = False) -> dict:
    """One sink-input as pactl reports it (only the keys we read)."""
    return {
        "index": index,
        "corked": corked,
        "properties": {"application.process.binary": binary},
    }


# ---------------------------------------------------------------------------
# wake_music_energy_multiplier — trivial Config wrapper
# ---------------------------------------------------------------------------


class TestWakeMusicEnergyMultiplier:
    def test_default(self, monkeypatch):
        monkeypatch.setattr(
            music_control.Config, "get_float",
            lambda key, default: default,
        )
        assert music_control.wake_music_energy_multiplier() == 1.5


# ---------------------------------------------------------------------------
# is_playing — PA cork state is the source of truth, not process existence.
# ---------------------------------------------------------------------------


class TestIsPlaying:

    def test_uncorked_known_binary_returns_true(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(42, "/usr/bin/mpv", corked=False),
            )),
        )
        assert music_control.is_playing() is True

    def test_corked_known_binary_returns_false(self, monkeypatch):
        # Daemon listening but not actively producing audio → corked.
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(42, "/usr/bin/spotifyd", corked=True),
            )),
        )
        assert music_control.is_playing() is False

    def test_uncorked_unknown_binary_returns_false(self, monkeypatch):
        # Some other audio app — not a media player we track.
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(42, "/usr/bin/firefox", corked=False),
            )),
        )
        assert music_control.is_playing() is False

    def test_basename_match_strips_full_path(self, monkeypatch):
        # The 2026-06-03 fix: PA reports absolute paths; we match basename
        # so custom-install locations like ~/.jarvis/spotify/bin/ work.
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(42, "/home/pi/.jarvis/spotify/bin/go-librespot",
                       corked=False),
            )),
        )
        assert music_control.is_playing() is True

    def test_empty_sink_inputs_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout="[]"),
        )
        assert music_control.is_playing() is False

    def test_empty_stdout_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=""),
        )
        assert music_control.is_playing() is False

    def test_invalid_json_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout="not json"),
        )
        assert music_control.is_playing() is False

    def test_non_zero_returncode_returns_false(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(returncode=1),
        )
        assert music_control.is_playing() is False

    @pytest.mark.parametrize("exc", [
        subprocess.TimeoutExpired(cmd=["pactl"], timeout=2.0),
        FileNotFoundError("pactl"),
        OSError("pactl borked"),
    ])
    def test_subprocess_exceptions_return_false(self, monkeypatch, exc):
        def explode(*a, **kw):
            raise exc
        monkeypatch.setattr(music_control.subprocess, "run", explode)
        assert music_control.is_playing() is False


# ---------------------------------------------------------------------------
# player_sink_input_ids — filter by binary basename against full union set.
# ---------------------------------------------------------------------------


class TestPlayerSinkInputIds:

    def test_returns_known_player_ids(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(10, "/usr/bin/mpv"),
                _entry(11, "/usr/bin/firefox"),         # ignored
                _entry(12, "/usr/bin/go-librespot"),
            )),
        )
        result = music_control.player_sink_input_ids()
        assert result == ["10", "12"]

    def test_empty_when_no_players(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(11, "/usr/bin/firefox"),
            )),
        )
        assert music_control.player_sink_input_ids() == []

    def test_returns_empty_on_subprocess_error(self, monkeypatch):
        def explode(*a, **kw):
            raise FileNotFoundError("pactl")
        monkeypatch.setattr(music_control.subprocess, "run", explode)
        assert music_control.player_sink_input_ids() == []


# ---------------------------------------------------------------------------
# player_sink_input_ids_for — variant with caller-supplied filter set.
# ---------------------------------------------------------------------------


class TestPlayerSinkInputIdsFor:

    def test_filters_to_provided_binaries(self, monkeypatch):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(10, "/usr/bin/mpv"),              # SIGSTOP class
                _entry(11, "/usr/bin/go-librespot"),     # MUTE class
            )),
        )
        assert music_control.player_sink_input_ids_for(("mpv",)) == ["10"]
        assert music_control.player_sink_input_ids_for(("go-librespot",)) == ["11"]
        assert music_control.player_sink_input_ids_for(
            ("mpv", "go-librespot")
        ) == ["10", "11"]


# ---------------------------------------------------------------------------
# ensure_duck_null_sink — load module only when not already present.
# ---------------------------------------------------------------------------


class TestEnsureDuckNullSink:

    def test_no_op_when_sink_already_present(self, monkeypatch):
        # Sink listing shows the null sink — must NOT call load-module.
        calls: list[list[str]] = []

        def runner(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:3] == ["pactl", "list", "short"]:
                return _proc(stdout="123\tjarvis_duck_null\tmodule-null-sink\n")
            return _proc()

        monkeypatch.setattr(music_control.subprocess, "run", runner)
        music_control.ensure_duck_null_sink()
        # Only the list call; no load-module.
        assert len(calls) == 1
        assert calls[0][:3] == ["pactl", "list", "short"]

    def test_loads_module_when_sink_absent(self, monkeypatch):
        calls: list[list[str]] = []

        def runner(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:3] == ["pactl", "list", "short"]:
                return _proc(stdout="456\tother\tmodule-other\n")
            return _proc()

        monkeypatch.setattr(music_control.subprocess, "run", runner)
        music_control.ensure_duck_null_sink()
        # Two calls: list, then load-module.
        assert len(calls) == 2
        assert calls[0][:3] == ["pactl", "list", "short"]
        assert "load-module" in calls[1]
        assert "module-null-sink" in calls[1]
        assert any("sink_name=jarvis_duck_null" in p for p in calls[1])

    def test_silent_on_list_failure(self, monkeypatch):
        def explode(cmd, **kw):
            raise FileNotFoundError("pactl")
        monkeypatch.setattr(music_control.subprocess, "run", explode)
        # Must not raise.
        music_control.ensure_duck_null_sink()

    def test_silent_on_load_failure(self, monkeypatch):
        def runner(cmd, **kw):
            if cmd[:3] == ["pactl", "list", "short"]:
                return _proc(stdout="(no jarvis sink here)")
            raise FileNotFoundError("load failed")
        monkeypatch.setattr(music_control.subprocess, "run", runner)
        # Must not raise.
        music_control.ensure_duck_null_sink()


# ---------------------------------------------------------------------------
# pause_active_playback — orchestrates ensure + move + mute + SIGSTOP.
# ---------------------------------------------------------------------------


class TestPauseActivePlayback:

    def test_full_sequence(self, monkeypatch):
        """All three pause mechanisms fire and the right targets receive
        the right action."""
        calls: list[list[str]] = []

        def runner(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:3] == ["pactl", "list", "short"]:
                return _proc(stdout="123\tjarvis_duck_null\tmodule-null-sink\n")
            if cmd[:4] == ["pactl", "-f", "json", "list"]:
                return _proc(stdout=_sink_inputs(
                    _entry(10, "/usr/bin/mpv"),              # SIGSTOP class
                    _entry(11, "/usr/bin/go-librespot"),     # MUTE class
                ))
            return _proc()

        monkeypatch.setattr(music_control.subprocess, "run", runner)
        music_control.pause_active_playback()

        # SIGSTOP-class sink-input 10 moved to null sink
        assert any(
            cmd[:2] == ["pactl", "move-sink-input"]
            and "10" in cmd and "jarvis_duck_null" in cmd
            for cmd in calls
        ), "mpv sink-input should be moved to null"

        # MUTE-only sink-input 11 muted (not moved)
        assert any(
            cmd[:2] == ["pactl", "set-sink-input-mute"]
            and "11" in cmd and "1" in cmd
            for cmd in calls
        ), "go-librespot sink-input should be muted"

        # mpv binary SIGSTOP'd
        assert any(
            cmd[:3] == ["pkill", "-STOP", "-x"] and "mpv" in cmd
            for cmd in calls
        ), "mpv process should be SIGSTOP'd"

        # go-librespot must NOT be SIGSTOP'd (protocol-sensitive)
        assert not any(
            cmd[:3] == ["pkill", "-STOP", "-x"] and "go-librespot" in cmd
            for cmd in calls
        ), "go-librespot must not be SIGSTOP'd — its HTTP API would hang"

    def test_continues_after_individual_failures(self, monkeypatch):
        """If a single pactl call raises, the others still execute."""
        calls: list[list[str]] = []

        def runner(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:3] == ["pactl", "list", "short"]:
                return _proc(stdout="jarvis_duck_null present")
            if cmd[:4] == ["pactl", "-f", "json", "list"]:
                return _proc(stdout=_sink_inputs(
                    _entry(10, "/usr/bin/mpv"),
                ))
            if cmd[:2] == ["pactl", "move-sink-input"]:
                raise subprocess.TimeoutExpired(cmd=cmd, timeout=2.0)
            return _proc()

        monkeypatch.setattr(music_control.subprocess, "run", runner)
        # Must not raise — the SIGSTOP step must still execute.
        music_control.pause_active_playback()
        assert any(
            cmd[:3] == ["pkill", "-STOP", "-x"] and "mpv" in cmd
            for cmd in calls
        )


# ---------------------------------------------------------------------------
# resume_active_playback — SIGCONT-first ordering matters: pulse buffers
# anything the resumed process emits regardless of where the sink-input
# lives, so SIGCONT then moves keeps the resume-audio gap minimal.
# ---------------------------------------------------------------------------


class TestResumeActivePlayback:

    def test_full_reverse_sequence(self, monkeypatch):
        calls: list[list[str]] = []

        def runner(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:4] == ["pactl", "-f", "json", "list"]:
                return _proc(stdout=_sink_inputs(
                    _entry(10, "/usr/bin/mpv"),
                    _entry(11, "/usr/bin/go-librespot"),
                ))
            return _proc()

        monkeypatch.setattr(music_control.subprocess, "run", runner)
        music_control.resume_active_playback()

        # SIGCONT issued before sink-input moves (ordering invariant)
        sigcont_idx = next(
            i for i, cmd in enumerate(calls)
            if cmd[:3] == ["pkill", "-CONT", "-x"] and "mpv" in cmd
        )
        move_idx = next(
            i for i, cmd in enumerate(calls)
            if cmd[:2] == ["pactl", "move-sink-input"] and "10" in cmd
        )
        assert sigcont_idx < move_idx, "SIGCONT must precede sink-input restore"

        # SIGSTOP-class sink-input 10 moved back to default sink
        assert any(
            cmd[:2] == ["pactl", "move-sink-input"]
            and "10" in cmd and "@DEFAULT_SINK@" in cmd
            for cmd in calls
        )

        # MUTE-only sink-input 11 unmuted
        assert any(
            cmd[:2] == ["pactl", "set-sink-input-mute"]
            and "11" in cmd and "0" in cmd
            for cmd in calls
        )
