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


def _entry_with_volume(
    index: int, binary: str, pct: int, corked: bool = False,
) -> dict:
    """A sink-input carrying pactl's per-channel volume block."""
    channel = {
        "value": int(65536 * pct / 100),
        "value_percent": f"{pct}%",
        "db": "0.00 dB",
    }
    e = _entry(index, binary, corked)
    e["volume"] = {"front-left": dict(channel), "front-right": dict(channel)}
    return e


@pytest.fixture(autouse=True)
def _reset_duck_state():
    """Duck state (saved volumes + self-playing flag) is module-level —
    reset around every test so ordering can't leak state."""
    music_control._saved_sink_input_volumes.clear()
    music_control.set_self_playing(False)
    yield
    music_control._saved_sink_input_volumes.clear()
    music_control.set_self_playing(False)


@pytest.fixture
def _duck_20(monkeypatch):
    """Pin music_duck_percent to the default 20 regardless of env/config."""
    monkeypatch.setattr(
        music_control.Config, "get_int",
        staticmethod(lambda key, default: 20 if key == "music_duck_percent" else default),
    )


@pytest.fixture
def _duck_0(monkeypatch):
    """music_duck_percent=0 — the legacy full-mute opt-out."""
    monkeypatch.setattr(
        music_control.Config, "get_int",
        staticmethod(lambda key, default: 0 if key == "music_duck_percent" else default),
    )


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


# ---------------------------------------------------------------------------
# Volume duck (mute-only class) — duck to music_duck_percent, save the
# original volume, restore it exactly once. SIGSTOP class is untouched.
# ---------------------------------------------------------------------------


class TestVolumeDuck:

    def _runner_with(self, sink_inputs_json: str, calls: list):
        def runner(cmd, **kw):
            calls.append(list(cmd))
            if cmd[:3] == ["pactl", "list", "short"]:
                return _proc(stdout="123\tjarvis_duck_null\tmodule-null-sink\n")
            if cmd[:4] == ["pactl", "-f", "json", "list"]:
                return _proc(stdout=sink_inputs_json)
            return _proc()
        return runner

    def test_mute_class_ducked_not_muted(self, monkeypatch, _duck_20):
        """go-librespot at 61% gets set-sink-input-volume 20%, no mute;
        the original 61% is saved for the restore."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with(_sink_inputs(
                _entry_with_volume(11, "/usr/bin/go-librespot", 61),
            ), calls),
        )
        music_control.pause_active_playback()

        assert ["pactl", "set-sink-input-volume", "11", "20%"] in calls
        assert not any(
            cmd[:2] == ["pactl", "set-sink-input-mute"] for cmd in calls
        ), "duck path must not fall back to mute when volume is readable"
        assert music_control._saved_sink_input_volumes == {"11": 61}

    def test_sigstop_class_unaffected_by_duck(self, monkeypatch, _duck_20):
        """mpv keeps the exact move-to-null + SIGSTOP treatment; the duck
        never touches its volume."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with(_sink_inputs(
                _entry_with_volume(10, "/usr/bin/mpv", 100),
            ), calls),
        )
        music_control.pause_active_playback()

        assert any(
            cmd[:2] == ["pactl", "move-sink-input"]
            and "10" in cmd and "jarvis_duck_null" in cmd
            for cmd in calls
        )
        assert any(
            cmd[:3] == ["pkill", "-STOP", "-x"] and "mpv" in cmd
            for cmd in calls
        )
        assert not any(
            cmd[:2] == ["pactl", "set-sink-input-volume"] for cmd in calls
        )
        assert music_control._saved_sink_input_volumes == {}

    def test_repeat_duck_does_not_reread_ducked_volume(self, monkeypatch, _duck_20):
        """The duck fires N times per turn (wake fire + follow-up
        re-ducks). The second fire sees the ALREADY-DUCKED 20% in pactl —
        it must keep the saved 61%, not overwrite it with 20 (which would
        make the restore 'restore' music to duck level)."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with(_sink_inputs(
                _entry_with_volume(11, "/usr/bin/go-librespot", 61),
            ), calls),
        )
        music_control.pause_active_playback()
        assert music_control._saved_sink_input_volumes == {"11": 61}

        # Second fire: pactl now reports the ducked volume.
        calls.clear()
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with(_sink_inputs(
                _entry_with_volume(11, "/usr/bin/go-librespot", 20),
            ), calls),
        )
        music_control.pause_active_playback()

        assert music_control._saved_sink_input_volumes == {"11": 61}, \
            "idempotence guard: saved original must survive repeat ducks"
        # Re-asserting the duck level itself is fine (and harmless).
        assert ["pactl", "set-sink-input-volume", "11", "20%"] in calls

    def test_resume_restores_saved_volume_and_clears(self, monkeypatch, _duck_20):
        """Restore sets the saved original back (NOT a flat 100% — the
        phone's volume slider maps onto this sink-input) and clears the
        dict so the next turn re-captures fresh."""
        music_control._saved_sink_input_volumes["11"] = 61
        calls: list[list[str]] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with(_sink_inputs(
                _entry_with_volume(11, "/usr/bin/go-librespot", 20),
            ), calls),
        )
        music_control.resume_active_playback()

        assert ["pactl", "set-sink-input-volume", "11", "61%"] in calls
        assert music_control._saved_sink_input_volumes == {}

    def test_resume_clears_saved_volumes_even_when_pactl_raises(self, monkeypatch):
        """Fail-open: a raising restore path must still clear the dict —
        a stale entry would poison the NEXT turn's idempotence guard with
        a volume from a dead sink-input."""
        music_control._saved_sink_input_volumes["11"] = 61

        def explode(cmd, **kw):
            raise FileNotFoundError("pactl")

        monkeypatch.setattr(music_control.subprocess, "run", explode)
        music_control.resume_active_playback()  # must not raise
        assert music_control._saved_sink_input_volumes == {}

    def test_duck_percent_zero_keeps_legacy_mute(self, monkeypatch, _duck_0):
        """music_duck_percent=0 is the explicit opt-out: full mute, no
        volume writes, nothing saved."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with(_sink_inputs(
                _entry_with_volume(11, "/usr/bin/go-librespot", 61),
            ), calls),
        )
        music_control.pause_active_playback()

        assert ["pactl", "set-sink-input-mute", "11", "1"] in calls
        assert not any(
            cmd[:2] == ["pactl", "set-sink-input-volume"] for cmd in calls
        )
        assert music_control._saved_sink_input_volumes == {}

    def test_unreadable_volume_falls_back_to_mute(self, monkeypatch, _duck_20):
        """No parsable volume block → mute that sink-input instead of
        ducking it (ducking without a saved original would strand the
        stream at duck level; the unconditional unmute on restore always
        recovers a mute)."""
        calls: list[list[str]] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with(_sink_inputs(
                _entry(11, "/usr/bin/go-librespot"),   # no "volume" key
            ), calls),
        )
        music_control.pause_active_playback()

        assert ["pactl", "set-sink-input-mute", "11", "1"] in calls
        assert not any(
            cmd[:2] == ["pactl", "set-sink-input-volume"] for cmd in calls
        )
        assert music_control._saved_sink_input_volumes == {}

    def test_resume_restores_multiple_saved_volumes(self, monkeypatch, _duck_20):
        """Two ducked streams (AirPlay + mpd) each get their own original
        back."""
        music_control._saved_sink_input_volumes.update({"11": 61, "12": 85})
        calls: list[list[str]] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            self._runner_with("[]", calls),
        )
        music_control.resume_active_playback()

        assert ["pactl", "set-sink-input-volume", "11", "61%"] in calls
        assert ["pactl", "set-sink-input-volume", "12", "85%"] in calls
        assert music_control._saved_sink_input_volumes == {}

    def test_volume_percent_parser_handles_mono_and_garbage(self):
        assert music_control._volume_percent_from_item(
            {"volume": {"mono": {"value_percent": "40%"}}}
        ) == 40
        # Max across mismatched channels.
        assert music_control._volume_percent_from_item(
            {"volume": {
                "front-left": {"value_percent": "30%"},
                "front-right": {"value_percent": "50%"},
            }}
        ) == 50
        assert music_control._volume_percent_from_item({}) is None
        assert music_control._volume_percent_from_item(
            {"volume": "garbage"}
        ) is None
        assert music_control._volume_percent_from_item(
            {"volume": {"mono": {"value_percent": "loud"}}}
        ) is None


# ---------------------------------------------------------------------------
# Self-playing cached flag — refreshed from the duck's enumeration.
# ---------------------------------------------------------------------------


class TestSelfPlayingFlag:

    def test_set_and_get(self):
        assert music_control.is_self_playing() is False
        music_control.set_self_playing(True)
        assert music_control.is_self_playing() is True
        music_control.set_self_playing(False)
        assert music_control.is_self_playing() is False

    def test_pause_refreshes_flag_true_on_uncorked_player(self, monkeypatch, _duck_20):
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda cmd, **kw: _proc(stdout=(
                "123\tjarvis_duck_null\tmodule-null-sink\n"
                if cmd[:3] == ["pactl", "list", "short"]
                else _sink_inputs(
                    _entry_with_volume(11, "/usr/bin/go-librespot", 61,
                                       corked=False),
                )
            )),
        )
        music_control.pause_active_playback()
        assert music_control.is_self_playing() is True

    def test_pause_refreshes_flag_false_when_idle(self, monkeypatch, _duck_20):
        """A stale True (music stopped externally since the last turn) is
        corrected by the next duck's enumeration."""
        music_control.set_self_playing(True)
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda cmd, **kw: _proc(stdout=(
                "123\tjarvis_duck_null\tmodule-null-sink\n"
                if cmd[:3] == ["pactl", "list", "short"]
                else "[]"
            )),
        )
        music_control.pause_active_playback()
        assert music_control.is_self_playing() is False

    def test_corked_daemon_does_not_set_flag(self, monkeypatch, _duck_20):
        """spotifyd idling (corked sink-input) is not self-playback."""
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda cmd, **kw: _proc(stdout=(
                "123\tjarvis_duck_null\tmodule-null-sink\n"
                if cmd[:3] == ["pactl", "list", "short"]
                else _sink_inputs(_entry(42, "/usr/bin/spotifyd", corked=True))
            )),
        )
        music_control.pause_active_playback()
        assert music_control.is_self_playing() is False


# ---------------------------------------------------------------------------
# _resolve_takeover_binary — SDK property wins; falls back to legacy map.
# ---------------------------------------------------------------------------


class TestResolveTakeoverBinary:

    def test_sdk_property_wins(self):
        """A command declaring takes_over_playback_binary opts in cleanly."""
        cmd = MagicMock()
        cmd.takes_over_playback_binary = "mpd"
        cmd.command_name = "wholly_unknown"
        assert music_control._resolve_takeover_binary(cmd) == "mpd"

    def test_legacy_map_fallback(self):
        """Existing music packages (no SDK property) resolve via command_name."""
        cmd = MagicMock(spec=["command_name"])  # spec excludes the property attr
        cmd.command_name = "audacy"
        assert music_control._resolve_takeover_binary(cmd) == "mpd"

        cmd.command_name = "spotify"
        assert music_control._resolve_takeover_binary(cmd) == "go-librespot"

        cmd.command_name = "music"
        assert music_control._resolve_takeover_binary(cmd) == "shairport-sync"

        cmd.command_name = "pandora"
        assert music_control._resolve_takeover_binary(cmd) == "mpv"

    def test_non_music_command_returns_none(self):
        """Weather / calendar / etc. don't trigger the takeover hook."""
        cmd = MagicMock(spec=["command_name"])
        cmd.command_name = "get_weather"
        assert music_control._resolve_takeover_binary(cmd) is None

    def test_empty_property_falls_through_to_map(self):
        """An empty-string property is treated as not-declared."""
        cmd = MagicMock()
        cmd.takes_over_playback_binary = ""
        cmd.command_name = "audacy"
        assert music_control._resolve_takeover_binary(cmd) == "mpd"


# ---------------------------------------------------------------------------
# stop_other_music_players — per-binary stop strategies + except guard.
# ---------------------------------------------------------------------------


class TestStopOtherMusicPlayers:

    def test_no_active_players_is_noop(self, monkeypatch):
        """Empty sink-input list short-circuits before any strategy runs."""
        called: list[str] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs()),
        )
        monkeypatch.setattr(
            music_control, "_pkill_int", lambda b: called.append(b),
        )
        music_control.stop_other_music_players()
        assert called == []

    def test_except_binary_is_skipped(self, monkeypatch):
        """The calling command's binary is left alone — re-tuning audacy
        shouldn't kill mpd."""
        pkilled: list[str] = []
        tcp_calls: list[str] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(10, "/usr/bin/mpd"),
                _entry(11, "/usr/bin/mpv"),
            )),
        )
        monkeypatch.setattr(
            music_control, "_stop_mpd_via_tcp",
            lambda: tcp_calls.append("mpd"),
        )
        monkeypatch.setattr(
            music_control, "_pkill_int",
            lambda b: pkilled.append(b),
        )
        music_control.stop_other_music_players(except_binary="mpd")
        assert tcp_calls == [], "mpd was excepted; TCP stop must not fire"
        assert pkilled == ["mpv"], "non-excepted mpv must be SIGINT'd"

    def test_mpd_uses_tcp_strategy(self, monkeypatch):
        """mpd's strategy is the in-protocol stop, not pkill."""
        pkilled: list[str] = []
        tcp_calls: list[str] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(10, "/usr/bin/mpd"),
            )),
        )
        monkeypatch.setattr(
            music_control, "_stop_mpd_via_tcp",
            lambda: tcp_calls.append("mpd"),
        )
        monkeypatch.setattr(
            music_control, "_pkill_int",
            lambda b: pkilled.append(b),
        )
        music_control.stop_other_music_players(except_binary="spotify_dummy")
        assert tcp_calls == ["mpd"]
        assert pkilled == []

    def test_go_librespot_uses_http_strategy(self, monkeypatch):
        """go-librespot is stopped via its HTTP API, not pkill."""
        http_calls: list[str] = []
        pkilled: list[str] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(10, "/usr/bin/go-librespot"),
            )),
        )
        monkeypatch.setattr(
            music_control, "_stop_go_librespot_via_http",
            lambda: http_calls.append("go-librespot"),
        )
        monkeypatch.setattr(
            music_control, "_pkill_int",
            lambda b: pkilled.append(b),
        )
        music_control.stop_other_music_players(except_binary=None)
        assert http_calls == ["go-librespot"]
        assert pkilled == []

    def test_pkill_strategy_for_subprocess_players(self, monkeypatch):
        """mpv / ffplay / shairport-sync all use the SIGINT strategy."""
        pkilled: list[str] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(10, "/usr/bin/mpv"),
                _entry(11, "/usr/bin/shairport-sync"),
            )),
        )
        monkeypatch.setattr(
            music_control, "_pkill_int",
            lambda b: pkilled.append(b),
        )
        music_control.stop_other_music_players()
        assert sorted(pkilled) == sorted(["mpv", "shairport-sync"])

    def test_unknown_active_binary_is_logged_not_killed(self, monkeypatch):
        """An active binary with no registered strategy is skipped (no
        wrong-shaped pkill against something unfamiliar)."""
        pkilled: list[str] = []
        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                # Not in _PLAYER_BINARIES at all → filtered out by
                # _active_player_binaries.
                _entry(10, "/usr/bin/something-weird"),
            )),
        )
        monkeypatch.setattr(
            music_control, "_pkill_int",
            lambda b: pkilled.append(b),
        )
        music_control.stop_other_music_players()
        assert pkilled == []

    def test_strategy_exception_does_not_abort_others(self, monkeypatch):
        """A raising strategy is logged; sibling strategies still run."""
        pkilled: list[str] = []

        def boom():
            raise RuntimeError("simulated TCP failure")

        monkeypatch.setattr(
            music_control.subprocess, "run",
            lambda *a, **kw: _proc(stdout=_sink_inputs(
                _entry(10, "/usr/bin/mpd"),
                _entry(11, "/usr/bin/mpv"),
            )),
        )
        monkeypatch.setattr(music_control, "_stop_mpd_via_tcp", boom)
        monkeypatch.setattr(
            music_control, "_pkill_int",
            lambda b: pkilled.append(b),
        )
        # Must not raise
        music_control.stop_other_music_players(except_binary=None)
        # mpv still got SIGINT despite mpd's strategy raising
        assert pkilled == ["mpv"]
