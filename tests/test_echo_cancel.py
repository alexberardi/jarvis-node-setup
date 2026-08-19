"""Tests for the PulseAudio daemon-side echo-cancel layer.

Every pactl round-trip is mocked (FakePactl routes on the subcommand),
so the suite is hermetic — no real PulseAudio. Coverage focus:

  * full engage lifecycle: default-device discovery → load-module (arg
    shape pinned against PA 16.x names) → EC-source verification →
    capture-stream move (PID match) → player sink-input moves;
  * disengage: moves back to the recorded masters, unloads ONLY the
    tracked module id, idempotent;
  * auto-fallback: every engage failure class rolls back partial
    wiring and sets the session-sticky ec_failed flag (no retry);
    the silent-capture watchdog triggers a background disengage;
  * module-id bookkeeping: we never unload a module we didn't load
    (boot normalization only touches rows carrying our
    source_name=jarvis_ec_source marker);
  * isolation: the public entry points never raise;
  * integration edges: music_control.set_self_playing drives
    engage/disengage, and the duck's restore path re-homes un-parked
    streams to the EC sink while engaged.
"""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock

import pytest

from core import echo_cancel, music_control


def _proc(stdout: str = "", returncode: int = 0) -> MagicMock:
    mock = MagicMock()
    mock.stdout = stdout
    mock.returncode = returncode
    return mock


def _source_output(index: int, *, pid: str | None = None,
                   binary: str = "") -> dict:
    props: dict = {}
    if pid is not None:
        props["application.process.id"] = pid
    if binary:
        props["application.process.binary"] = binary
    return {"index": index, "properties": props}


class FakePactl:
    """subprocess.run stand-in routing pactl subcommands to canned
    responses. Non-pactl commands (amixer, pkill from neighbouring
    code paths) get a harmless rc=1. Records every call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.default_source = "alsa_input.respeaker"
        self.default_sink = "alsa_output.respeaker"
        self.load_rc = 0
        self.load_stdout = "536\n"
        self.sources_short = (
            "48\talsa_input.respeaker\tmodule-alsa-card.c\ts16le\tIDLE\n"
            "49\tjarvis_ec_source\tmodule-echo-cancel.c\ts16le\tIDLE\n"
        )
        self.source_outputs: list[dict] = [
            _source_output(21, pid=str(os.getpid())),
            _source_output(22, pid="99999", binary="/usr/bin/shairport-sync"),
        ]
        self.fail_source_output_moves: set[str] = set()
        self.fail_sink_input_moves: set[str] = set()
        self.unload_rc = 0
        self.modules_short = ""

    @property
    def pactl_args(self) -> list[list[str]]:
        return [c[1:] for c in self.calls if c and c[0] == "pactl"]

    def args_starting(self, *prefix: str) -> list[list[str]]:
        n = len(prefix)
        return [a for a in self.pactl_args if a[:n] == list(prefix)]

    def __call__(self, cmd, capture_output=True, text=True, timeout=None):
        self.calls.append(list(cmd))
        if not cmd or cmd[0] != "pactl":
            return _proc("", 1)
        args = cmd[1:]
        if args == ["get-default-source"]:
            return _proc(self.default_source + "\n")
        if args == ["get-default-sink"]:
            return _proc(self.default_sink + "\n")
        if args and args[0] == "load-module":
            return _proc(self.load_stdout, self.load_rc)
        if args == ["list", "short", "sources"]:
            return _proc(self.sources_short)
        if args == ["-f", "json", "list", "source-outputs"]:
            return _proc(json.dumps(self.source_outputs))
        if args and args[0] == "move-source-output":
            rc = 1 if args[1] in self.fail_source_output_moves else 0
            return _proc("", rc)
        if args and args[0] == "move-sink-input":
            rc = 1 if args[1] in self.fail_sink_input_moves else 0
            return _proc("", rc)
        if args and args[0] == "unload-module":
            return _proc("", self.unload_rc)
        if args == ["list", "modules", "short"]:
            return _proc(self.modules_short)
        return _proc("", 0)


@pytest.fixture(autouse=True)
def _reset_ec_state():
    echo_cancel._reset_state_for_tests()
    yield
    echo_cancel._reset_state_for_tests()


@pytest.fixture
def pactl(monkeypatch) -> FakePactl:
    fake = FakePactl()
    monkeypatch.setattr(echo_cancel.subprocess, "run", fake)
    # Config default-path: pin enabled=True regardless of env/config.json.
    monkeypatch.setattr(
        echo_cancel.Config, "get_bool",
        staticmethod(lambda key, default=None: default),
    )
    # Player enumeration (lazy-imported from music_control).
    monkeypatch.setattr(
        music_control, "player_sink_input_ids_for",
        lambda binaries: ["7", "9"],
    )
    return fake


@pytest.fixture
def sync_fallback_thread(monkeypatch):
    """Run the watchdog's fallback thread synchronously so tests are
    deterministic."""

    class _SyncThread:
        def __init__(self, target=None, kwargs=None, **kw) -> None:
            self._target = target
            self._kwargs = kwargs or {}

        def start(self) -> None:
            self._target(**self._kwargs)

    monkeypatch.setattr(echo_cancel.threading, "Thread", _SyncThread)


def _engaged_pactl(pactl: FakePactl) -> FakePactl:
    echo_cancel.engage_for_music(trigger="test")
    assert echo_cancel.is_active()
    return pactl


# ---------------------------------------------------------------------------
# Engage
# ---------------------------------------------------------------------------


class TestEngage:

    def test_happy_path_loads_module_with_pa16_args(self, pactl):
        echo_cancel.engage_for_music(trigger="deferred_play")

        assert echo_cancel.is_active()
        loads = pactl.args_starting("load-module")
        assert loads == [[
            "load-module", "module-echo-cancel",
            "aec_method=webrtc",
            "source_master=alsa_input.respeaker",
            "sink_master=alsa_output.respeaker",
            "source_name=jarvis_ec_source",
            "sink_name=jarvis_ec_sink",
            "use_master_format=1",
        ]]
        assert echo_cancel._module_id == "536"

    def test_happy_path_moves_only_our_capture_stream(self, pactl):
        """PID match: our source-output (21) moves; the foreign one (22)
        is untouched."""
        echo_cancel.engage_for_music(trigger="test")

        moves = pactl.args_starting("move-source-output")
        assert moves == [["move-source-output", "21", "jarvis_ec_source"]]

    def test_happy_path_moves_player_sink_inputs_to_ec_sink(self, pactl):
        echo_cancel.engage_for_music(trigger="test")

        moves = pactl.args_starting("move-sink-input")
        assert moves == [
            ["move-sink-input", "7", "jarvis_ec_sink"],
            ["move-sink-input", "9", "jarvis_ec_sink"],
        ]
        assert echo_cancel._moved_sink_inputs == ["7", "9"]

    def test_binary_fallback_when_pid_property_absent(self, pactl):
        """No application.process.id anywhere → fall back to the
        python-binary match."""
        pactl.source_outputs = [
            _source_output(31, binary="/opt/jarvis-node/.venv/bin/python3"),
            _source_output(32, binary="/usr/bin/shairport-sync"),
        ]
        echo_cancel.engage_for_music(trigger="test")

        moves = pactl.args_starting("move-source-output")
        assert moves == [["move-source-output", "31", "jarvis_ec_source"]]

    def test_engage_is_idempotent_while_active(self, pactl):
        echo_cancel.engage_for_music(trigger="test")
        echo_cancel.engage_for_music(trigger="test")

        assert len(pactl.args_starting("load-module")) == 1

    def test_disabled_by_config_makes_no_pactl_calls(self, pactl, monkeypatch):
        monkeypatch.setattr(
            echo_cancel.Config, "get_bool",
            staticmethod(lambda key, default=None: False),
        )
        echo_cancel.engage_for_music(trigger="test")

        assert not echo_cancel.is_active()
        assert pactl.pactl_args == []

    def test_restore_sink_target_follows_active_state(self, pactl):
        assert echo_cancel.restore_sink_target() == "@DEFAULT_SINK@"
        echo_cancel.engage_for_music(trigger="test")
        assert echo_cancel.restore_sink_target() == "jarvis_ec_sink"


# ---------------------------------------------------------------------------
# Engage failures → sticky fallback
# ---------------------------------------------------------------------------


class TestEngageFailures:

    def test_load_module_failure_is_sticky_no_retry(self, pactl):
        pactl.load_rc = 1
        echo_cancel.engage_for_music(trigger="test")

        assert not echo_cancel.is_active()
        assert echo_cancel._failed

        echo_cancel.engage_for_music(trigger="test")
        # Sticky: no second load-module attempt this session.
        assert len(pactl.args_starting("load-module")) == 1

    def test_ec_source_missing_unloads_our_module(self, pactl):
        pactl.sources_short = (
            "48\talsa_input.respeaker\tmodule-alsa-card.c\ts16le\tIDLE\n"
        )
        echo_cancel.engage_for_music(trigger="test")

        assert not echo_cancel.is_active()
        assert echo_cancel._failed
        assert pactl.args_starting("unload-module") == [
            ["unload-module", "536"],
        ]

    def test_no_source_output_found_rolls_back(self, pactl):
        pactl.source_outputs = []
        echo_cancel.engage_for_music(trigger="test")

        assert not echo_cancel.is_active()
        assert echo_cancel._failed
        assert pactl.args_starting("unload-module") == [
            ["unload-module", "536"],
        ]
        assert pactl.args_starting("move-source-output") == []

    def test_source_output_move_failure_rolls_back_prior_moves(self, pactl):
        pactl.source_outputs = [
            _source_output(21, pid=str(os.getpid())),
            _source_output(25, pid=str(os.getpid())),
        ]
        pactl.fail_source_output_moves = {"25"}
        echo_cancel.engage_for_music(trigger="test")

        assert not echo_cancel.is_active()
        assert echo_cancel._failed
        moves = pactl.args_starting("move-source-output")
        # 21 → EC source, 25 → EC source (fails), 21 rolled back to master.
        assert moves == [
            ["move-source-output", "21", "jarvis_ec_source"],
            ["move-source-output", "25", "jarvis_ec_source"],
            ["move-source-output", "21", "alsa_input.respeaker"],
        ]
        assert pactl.args_starting("unload-module") == [
            ["unload-module", "536"],
        ]

    def test_sink_input_move_failure_is_nonfatal(self, pactl):
        pactl.fail_sink_input_moves = {"7", "9"}
        echo_cancel.engage_for_music(trigger="test")

        # Reference-stream moves are best-effort: still engaged.
        assert echo_cancel.is_active()
        assert not echo_cancel._failed
        assert echo_cancel._moved_sink_inputs == []

    def test_stale_ec_default_source_fails_without_stacking(self, pactl):
        pactl.default_source = "jarvis_ec_source"
        echo_cancel.engage_for_music(trigger="test")

        assert not echo_cancel.is_active()
        assert echo_cancel._failed
        assert pactl.args_starting("load-module") == []

    def test_engage_crash_is_contained_and_sticky(self, pactl, monkeypatch):
        monkeypatch.setattr(
            echo_cancel, "_default_device",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        echo_cancel.engage_for_music(trigger="test")  # must not raise

        assert not echo_cancel.is_active()
        assert echo_cancel._failed


# ---------------------------------------------------------------------------
# Disengage
# ---------------------------------------------------------------------------


class TestDisengage:

    def test_restores_streams_and_unloads_tracked_module(self, pactl):
        _engaged_pactl(pactl)
        pactl.calls.clear()

        echo_cancel.disengage_for_music(trigger="music_stopped")

        assert not echo_cancel.is_active()
        assert pactl.args_starting("move-source-output") == [
            ["move-source-output", "21", "alsa_input.respeaker"],
        ]
        assert pactl.args_starting("move-sink-input") == [
            ["move-sink-input", "7", "alsa_output.respeaker"],
            ["move-sink-input", "9", "alsa_output.respeaker"],
        ]
        assert pactl.args_starting("unload-module") == [
            ["unload-module", "536"],
        ]

    def test_disengage_is_idempotent(self, pactl):
        _engaged_pactl(pactl)
        echo_cancel.disengage_for_music(trigger="test")
        pactl.calls.clear()

        echo_cancel.disengage_for_music(trigger="test")

        assert pactl.pactl_args == []

    def test_disengage_never_unloads_untracked_modules(self, pactl):
        """Bookkeeping invariant: every unload-module call this module
        makes carries the id OUR load-module returned."""
        _engaged_pactl(pactl)
        echo_cancel.disengage_for_music(trigger="test")

        for args in pactl.args_starting("unload-module"):
            assert args[1] == "536"

    def test_disengage_crash_is_contained(self, pactl, monkeypatch):
        _engaged_pactl(pactl)
        monkeypatch.setattr(
            echo_cancel, "_pactl",
            MagicMock(side_effect=RuntimeError("pactl gone")),
        )
        echo_cancel.disengage_for_music(trigger="test")  # must not raise
        # Flag flipped before the crash — telemetry reads inactive.
        assert not echo_cancel.is_active()

    def test_disengage_does_not_reset_sticky_failure(self, pactl):
        _engaged_pactl(pactl)
        echo_cancel._failed = True
        echo_cancel.disengage_for_music(trigger="test")

        assert echo_cancel._failed


# ---------------------------------------------------------------------------
# Silent-capture watchdog → auto-fallback
# ---------------------------------------------------------------------------


class TestWatchdog:

    def test_two_seconds_of_silence_triggers_fallback(
        self, pactl, sync_fallback_thread,
    ):
        _engaged_pactl(pactl)

        for _ in range(24):
            echo_cancel.note_capture_chunk(0.0)
        assert echo_cancel.is_active()          # 1.92 s — not yet
        assert not echo_cancel._failed

        echo_cancel.note_capture_chunk(0.0)      # 25th chunk = 2 s

        assert not echo_cancel.is_active()       # disengaged
        assert echo_cancel._failed               # sticky
        assert pactl.args_starting("unload-module") == [
            ["unload-module", "536"],
        ]

    def test_loud_chunk_resets_silence_run(self, pactl, sync_fallback_thread):
        _engaged_pactl(pactl)

        for _ in range(24):
            echo_cancel.note_capture_chunk(0.0)
        echo_cancel.note_capture_chunk(850.0)    # healthy capture
        for _ in range(24):
            echo_cancel.note_capture_chunk(0.0)

        assert echo_cancel.is_active()
        assert not echo_cancel._failed

    def test_watch_window_expires_after_healthy_start(
        self, pactl, sync_fallback_thread,
    ):
        """Silence AFTER the first ~10 s of healthy scored audio is a
        quiet room, not a dead capture — no fallback."""
        _engaged_pactl(pactl)

        for _ in range(echo_cancel._WATCH_CHUNKS):
            echo_cancel.note_capture_chunk(850.0)
        for _ in range(60):
            echo_cancel.note_capture_chunk(0.0)

        assert echo_cancel.is_active()
        assert not echo_cancel._failed

    def test_note_capture_chunk_is_noop_when_inactive(self, pactl):
        for _ in range(60):
            echo_cancel.note_capture_chunk(0.0)

        assert not echo_cancel._failed
        assert pactl.pactl_args == []

    def test_fallback_fires_only_once(self, pactl, sync_fallback_thread):
        _engaged_pactl(pactl)
        for _ in range(30):
            echo_cancel.note_capture_chunk(0.0)

        unloads = pactl.args_starting("unload-module")
        assert unloads == [["unload-module", "536"]]

    def test_sticky_failure_blocks_reengage_after_fallback(
        self, pactl, sync_fallback_thread,
    ):
        _engaged_pactl(pactl)
        for _ in range(25):
            echo_cancel.note_capture_chunk(0.0)
        pactl.calls.clear()

        echo_cancel.engage_for_music(trigger="next_music_start")

        assert not echo_cancel.is_active()
        assert pactl.args_starting("load-module") == []


# ---------------------------------------------------------------------------
# Boot normalization
# ---------------------------------------------------------------------------


class TestNormalizeOnStartup:

    def test_unloads_only_modules_with_our_marker(self, pactl):
        pactl.modules_short = (
            "12\tmodule-loopback\tsource=bluez_source.AA_BB\n"
            "30\tmodule-echo-cancel\taec_method=webrtc "
            "source_name=someone_elses_source\n"
            "31\tmodule-echo-cancel\taec_method=webrtc "
            "source_master=alsa_input.respeaker "
            "source_name=jarvis_ec_source sink_name=jarvis_ec_sink\n"
        )
        echo_cancel.normalize_on_startup()

        assert pactl.args_starting("unload-module") == [
            ["unload-module", "31"],
        ]

    def test_nothing_to_unload_is_a_noop(self, pactl):
        pactl.modules_short = (
            "12\tmodule-loopback\tsource=bluez_source.AA_BB\n"
        )
        echo_cancel.normalize_on_startup()

        assert pactl.args_starting("unload-module") == []

    def test_listing_failure_is_contained(self, pactl, monkeypatch):
        monkeypatch.setattr(
            echo_cancel, "_pactl",
            MagicMock(side_effect=RuntimeError("no PA")),
        )
        echo_cancel.normalize_on_startup()  # must not raise


# ---------------------------------------------------------------------------
# Integration edges (music_control ↔ echo_cancel)
# ---------------------------------------------------------------------------


@pytest.fixture
def _quiet_music_control(monkeypatch):
    """Silence music_control's own side channels (PGA amixer) and reset
    its self-playing flag around the test."""
    music_control._self_playing = False
    music_control._pga_engaged = False
    music_control._saved_normal_pga_percent = None
    monkeypatch.setattr(
        music_control.audio_volume, "get_capture_pga_percent", lambda: None,
    )
    monkeypatch.setattr(
        music_control.audio_volume, "set_capture_pga_percent", lambda pct: False,
    )
    yield
    music_control._self_playing = False
    music_control._pga_engaged = False
    music_control._saved_normal_pga_percent = None


class TestSelfPlayingEdges:

    def test_false_to_true_edge_engages(self, _quiet_music_control, monkeypatch):
        engage = MagicMock()
        disengage = MagicMock()
        monkeypatch.setattr(echo_cancel, "engage_for_music", engage)
        monkeypatch.setattr(echo_cancel, "disengage_for_music", disengage)

        music_control.set_self_playing(True, trigger="deferred_play")

        engage.assert_called_once_with(trigger="deferred_play")
        disengage.assert_not_called()

    def test_repeat_true_is_not_an_edge(self, _quiet_music_control, monkeypatch):
        engage = MagicMock()
        monkeypatch.setattr(echo_cancel, "engage_for_music", engage)
        monkeypatch.setattr(
            echo_cancel, "disengage_for_music", MagicMock(),
        )

        music_control.set_self_playing(True, trigger="a")
        music_control.set_self_playing(True, trigger="b")

        engage.assert_called_once()

    def test_true_to_false_edge_disengages(self, _quiet_music_control, monkeypatch):
        monkeypatch.setattr(echo_cancel, "engage_for_music", MagicMock())
        disengage = MagicMock()
        monkeypatch.setattr(echo_cancel, "disengage_for_music", disengage)

        music_control.set_self_playing(True, trigger="deferred_play")
        music_control.set_self_playing(False, trigger="duck_enumeration")

        disengage.assert_called_once_with(trigger="duck_enumeration")

    def test_edge_hook_crash_never_breaks_set_self_playing(
        self, _quiet_music_control, monkeypatch,
    ):
        monkeypatch.setattr(
            echo_cancel, "engage_for_music",
            MagicMock(side_effect=RuntimeError("boom")),
        )
        music_control.set_self_playing(True, trigger="test")  # must not raise
        assert music_control.is_self_playing()


class TestDuckRestoreTarget:

    def test_resume_restores_parked_streams_to_ec_sink_while_active(
        self, pactl, _quiet_music_control, monkeypatch,
    ):
        """The duck's restore path must not strip the AEC's reference:
        un-parked SIGSTOP-class sink-inputs return to jarvis_ec_sink
        while EC is engaged."""
        _engaged_pactl(pactl)
        # music_control shares the global subprocess.run fake; route its
        # enumerations: one parked SIGSTOP-class stream, nothing else.
        monkeypatch.setattr(
            music_control, "player_sink_input_ids_for",
            lambda binaries: ["4"]
            if binaries == music_control._SIGSTOP_PLAYER_BINARIES else [],
        )
        monkeypatch.setattr(music_control.subprocess, "run", pactl)
        pactl.calls.clear()

        music_control.resume_active_playback()

        assert ["move-sink-input", "4", "jarvis_ec_sink"] in pactl.pactl_args

    def test_resume_uses_default_sink_when_inactive(
        self, pactl, _quiet_music_control, monkeypatch,
    ):
        monkeypatch.setattr(
            music_control, "player_sink_input_ids_for",
            lambda binaries: ["4"]
            if binaries == music_control._SIGSTOP_PLAYER_BINARIES else [],
        )
        monkeypatch.setattr(music_control.subprocess, "run", pactl)

        music_control.resume_active_playback()

        assert ["move-sink-input", "4", "@DEFAULT_SINK@"] in pactl.pactl_args
