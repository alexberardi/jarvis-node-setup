"""PulseAudio daemon-side echo cancellation for wake-during-music.

The layer: while the node's own music plays, load PA's
``module-echo-cancel`` (webrtc backend, native in the PA daemon), route
our capture stream through the cancelled source (``jarvis_ec_source``)
and route the music players' sink-inputs through the cancelled sink
(``jarvis_ec_sink``) so the AEC gets its reference signal. When music
stops, tear it all down and return to raw capture.

Why this is NOT the June-2026 failure being retried
---------------------------------------------------
The June 2026 attempt ran Speex AEC IN-PROCESS on the Python side and
failed hard (1 dB of a 9.6 dB ceiling, 8 s wake stall, calibration
starvation — prds/wake-during-music/findings-2026-06-03.md); it was
fully removed. This layer is different in kind:

  * the AEC runs inside the PulseAudio daemon (C, webrtc audio
    processing), not in the Python wake loop — zero cycles on the
    capture thread;
  * ``libspeexdsp1`` + ``libwebrtc-audio-processing`` are already
    installed by install.sh on every node;
  * the whole fleet already captures through PulseAudio (PortAudio's
    "pulse" device), so a ``move-source-output`` re-homes our capture
    transparently — no capture-path rewrite.

The open risk is webrtc-AEC CPU on the Pi Zero 2W. Mitigation is
structural: the module is loaded ONLY while self-playback is active
(the same False→True / True→False edges that drive the mic-gain PGA
profile in ``core.music_control``), and any failure — load, source
materialization, stream move, or capture going silent right after
engage — triggers an automatic fallback to raw capture with a
session-sticky ``ec_failed`` flag (no retry until node restart).

Doctrine (full-approach, per-layer toggles, fail-open)
------------------------------------------------------
  * ``music_echo_cancel_enabled`` (Config, default **true**) disables
    the whole layer; every engage/disengage/fallback/skip is logged.
  * This layer never suppresses a wake decision — it only changes what
    audio the detector hears. CC verification (clip phrase-match,
    soft-bias) remains the backstop for false fires.
  * ``echo_cancel_active`` rides on the per-fire "Wake fired" log so
    the layer can be peeled back with data.

Stream-routing decisions (documented on purpose)
------------------------------------------------
  * **Music players** (``music_control._PLAYER_BINARIES`` sink-inputs)
    are moved to ``jarvis_ec_sink`` at engage time — they are the echo
    the AEC must cancel. The duck's volume logic is unaffected:
    ``set-sink-input-volume`` / ``set-sink-input-mute`` address the
    sink-input by id and follow the stream wherever it lives. The
    duck's move-to-null park (SIGSTOP class) also still works; its
    restore consults :func:`restore_sink_target` so un-parked streams
    return to the EC sink while EC is active instead of landing on the
    raw sink and losing the reference path.
  * **TTS aplay streams stay on the raw sink** (deliberate): the
    sink-keepalive stream in scripts/main.py is ALSO aplay, so moving
    "aplay" sink-inputs would drag the keepalive onto the EC sink and
    couple its lifetime to ours; TTS echo is separately handled by the
    wake-pause + barge-in machinery, and TTS never overlaps open-mic
    wake scoring the way music does. Residual TTS echo is accepted.
  * **Bluetooth**: services/bluetooth_scan_handler.py loads its own
    ``module-loopback`` instances. Module bookkeeping here never
    touches modules other than (a) the id returned by OUR load-module
    call, or (b) at boot, ``module-echo-cancel`` rows whose args
    contain our unique ``source_name=jarvis_ec_source`` marker.
  * **New sink-inputs created while EC is active** (e.g. a player
    re-opening its stream between tracks) land on the default sink and
    are not chased onto the EC sink until the next engage edge — known
    gap, accepted for now; the per-fire telemetry will show whether it
    matters in practice.

Failure containment
-------------------
Every public function is exception-proof — the wake loop must never
die from EC machinery. The silent-capture watchdog
(:func:`note_capture_chunk`) is fed from the wake loop's existing
per-chunk RMS (the cheapest available signal) and runs its fallback
disengage on a daemon thread so pactl round-trips never stall wake
scoring.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time

from jarvis_log_client import JarvisLogger

from utils.config_service import Config


logger = JarvisLogger(service="jarvis-node")


# Names are the collision-proof markers for everything we own in PA.
EC_SOURCE_NAME = "jarvis_ec_source"
EC_SINK_NAME = "jarvis_ec_sink"
_EC_MODULE_NAME = "module-echo-cancel"

_PACTL_TIMEOUT = 2.0

# Silent-capture watchdog tuning. The wake loop feeds one raw-capture
# RMS per 80 ms scored chunk; a healthy mic in a silent room still
# reads tens of RMS counts, so < 1.0 means the capture path is dead
# (exact zeros from a wedged EC source). Counted in CHUNKS, not wall
# time, because the wake loop stops scoring during a turn — the watch
# window must not expire while no chunks flow.
_SILENT_RMS = 1.0
_SILENT_CHUNKS_LIMIT = 25   # ~2 s of consecutive 80 ms silent chunks
_WATCH_CHUNKS = 125         # watch the first ~10 s of scored audio

# ── Module state ───────────────────────────────────────────────────────
# _lock serializes engage/disengage (duck-time edge in a bg executor,
# deferred-play hook, fallback thread, startup can race). The watchdog
# counters are deliberately read/written WITHOUT the lock from the wake
# loop's per-chunk hot path — plain int/bool ops are GIL-atomic and an
# off-by-one chunk is irrelevant; taking a lock 12.5×/s in the scoring
# loop is not.
_lock = threading.Lock()
_active: bool = False
_failed: bool = False            # session-sticky; reset only by restart
_module_id: str | None = None    # OUR load-module id — the only id we unload
_master_source: str | None = None
_master_sink: str | None = None
_moved_source_outputs: list[str] = []
_moved_sink_inputs: list[str] = []
_watch_chunks_remaining: int = 0
_consec_silent_chunks: int = 0
_fallback_started: bool = False


def is_active() -> bool:
    """Cheap flag read for per-fire telemetry (``echo_cancel_active``)."""
    return _active


def restore_sink_target() -> str:
    """Where the duck's restore path should move un-parked sink-inputs.

    ``jarvis_ec_sink`` while EC is engaged (keeps the AEC's reference
    signal intact across a wake turn), the PA default otherwise.
    Consumed by ``music_control.resume_active_playback``.
    """
    return EC_SINK_NAME if _active else "@DEFAULT_SINK@"


# ── pactl plumbing ─────────────────────────────────────────────────────


def _pactl(*args: str) -> tuple[int, str, str | None]:
    """One pactl round-trip → ``(rc, stdout, failure_class)``.

    ``failure_class`` is None on success; otherwise a short string
    (``timeout`` / ``exec_error:<Exc>`` / ``nonzero_exit:<rc>``) fit
    for the fallback telemetry.
    """
    try:
        r = subprocess.run(
            ["pactl", *args],
            capture_output=True, text=True, timeout=_PACTL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"
    except (FileNotFoundError, OSError) as e:
        return -1, "", f"exec_error:{type(e).__name__}"
    if r.returncode != 0:
        return r.returncode, r.stdout or "", f"nonzero_exit:{r.returncode}"
    return 0, r.stdout or "", None


def _default_device(kind: str) -> tuple[str | None, str | None]:
    """``pactl get-default-source|sink`` → ``(name, failure_class)``."""
    _rc, out, fail = _pactl(f"get-default-{kind}")
    if fail is not None:
        return None, fail
    name = out.strip()
    return (name, None) if name else (None, "empty")


def _unload_module(module_id: str) -> bool:
    """Unload ONE module by id. Only ever called with our own tracked
    id or a boot-time row positively identified by our source_name
    marker — never someone else's module (bluetooth loads its own)."""
    _rc, _out, fail = _pactl("unload-module", module_id)
    return fail is None


def _our_source_output_ids() -> list[str]:
    """Find OUR capture stream(s) among PA source-outputs.

    Primary match: ``application.process.id`` == our PID (exact,
    collision-proof). Fallback: process binary basename starting with
    "python" — the spec'd weaker match, only used when PA didn't
    report a PID property. On a node only our process captures, so the
    fallback is safe in practice; the match tier is visible in logs
    via the returned ids' count.
    """
    _rc, out, fail = _pactl("-f", "json", "list", "source-outputs")
    if fail is not None:
        return []
    try:
        items = json.loads(out or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(items, list):
        return []
    pid = str(os.getpid())
    pid_ids: list[str] = []
    binary_ids: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        idx = item.get("index")
        if idx is None:
            continue
        props = item.get("properties") or {}
        if props.get("application.process.id") == pid:
            pid_ids.append(str(idx))
        elif os.path.basename(
            props.get("application.process.binary") or ""
        ).startswith("python"):
            binary_ids.append(str(idx))
    return pid_ids if pid_ids else binary_ids


def _player_sink_input_ids() -> list[str]:
    """Sink-inputs of known music players (the AEC reference streams).

    Lazy import: ``music_control`` calls into this module at its
    self-playing edges — a top-level cross-import would be circular.
    Reuses music_control's enumeration so the player-binary knowledge
    stays in one place.
    """
    from core.music_control import _PLAYER_BINARIES, player_sink_input_ids_for

    return player_sink_input_ids_for(_PLAYER_BINARIES)


# ── Engage / disengage ─────────────────────────────────────────────────


def engage_for_music(trigger: str = "unspecified") -> None:
    """Load + wire the PA echo-cancel module. Never raises.

    Called on the self-playing False→True edge (the PGA profile's
    hooks in ``music_control.set_self_playing``). No-ops when the
    layer is config-disabled, sticky-failed, or already engaged.
    """
    global _failed
    try:
        _engage(trigger)
    except Exception as e:  # containment guarantee — wake loop safety
        _failed = True
        try:
            logger.warning(
                "echo cancel engage crashed — sticky-disabled until node "
                "restart, raw capture unaffected",
                failure_class=f"engage_crashed:{type(e).__name__}",
                error=str(e),
                trigger=trigger,
            )
        except Exception:
            pass


def _engage(trigger: str) -> None:
    global _active, _module_id, _master_source, _master_sink
    global _moved_source_outputs, _moved_sink_inputs
    global _watch_chunks_remaining, _consec_silent_chunks, _fallback_started

    if not Config.get_bool("music_echo_cancel_enabled", True):
        logger.info(
            "echo cancel disabled by config — engage skipped",
            trigger=trigger,
        )
        return
    with _lock:
        if _failed:
            logger.info(
                "echo cancel engage skipped (sticky ec_failed — no retry "
                "until node restart)",
                trigger=trigger,
            )
            return
        if _active:
            return
        t0 = time.monotonic()

        master_source, fail = _default_device("source")
        if master_source is None:
            _fail_locked(f"default_source_lookup:{fail}", trigger, t0)
            return
        master_sink, fail = _default_device("sink")
        if master_sink is None:
            _fail_locked(f"default_sink_lookup:{fail}", trigger, t0)
            return
        if master_source == EC_SOURCE_NAME or master_sink == EC_SINK_NAME:
            # A stale EC module survived (startup normalization missed
            # it?) — never stack a second one on top of the first.
            _fail_locked("stale_ec_default", trigger, t0)
            return

        # Parameter names verified against PA 16.x (bookworm/trixie)
        # module-echo-cancel docs: aec_method, source_master,
        # sink_master, source_name, sink_name, use_master_format.
        _rc, out, fail = _pactl(
            "load-module", _EC_MODULE_NAME,
            "aec_method=webrtc",
            f"source_master={master_source}",
            f"sink_master={master_sink}",
            f"source_name={EC_SOURCE_NAME}",
            f"sink_name={EC_SINK_NAME}",
            "use_master_format=1",
        )
        if fail is not None:
            _fail_locked(f"load_module:{fail}", trigger, t0)
            return
        module_id = out.strip()
        if not module_id.isdigit():
            _fail_locked("load_module_bad_id", trigger, t0)
            return

        # Verify the cancelled source actually materialized — a load
        # that "succeeds" without producing the source is the classic
        # missing-webrtc-lib failure shape.
        _rc, out, fail = _pactl("list", "short", "sources")
        if fail is not None or EC_SOURCE_NAME not in out:
            _unload_module(module_id)
            _fail_locked(
                f"ec_source_missing:{fail}" if fail else "ec_source_missing",
                trigger, t0,
            )
            return

        # Move OUR capture stream onto the cancelled source. This move
        # is the whole point — failure here is fatal (rollback + sticky).
        source_output_ids = _our_source_output_ids()
        if not source_output_ids:
            _unload_module(module_id)
            _fail_locked("source_output_not_found", trigger, t0)
            return
        moved_source_outputs: list[str] = []
        for so_id in source_output_ids:
            _rc, _out, fail = _pactl(
                "move-source-output", so_id, EC_SOURCE_NAME,
            )
            if fail is not None:
                for done in moved_source_outputs:
                    _pactl("move-source-output", done, master_source)
                _unload_module(module_id)
                _fail_locked(f"source_output_move:{fail}", trigger, t0)
                return
            moved_source_outputs.append(so_id)

        # Route music sink-inputs through the EC sink (reference
        # signal). Best-effort per stream: a stream that fails to move
        # just doesn't get cancelled — worse AEC, not a broken node —
        # so it's logged, not fatal.
        moved_sink_inputs: list[str] = []
        failed_sink_input_moves: list[str] = []
        for si_id in _player_sink_input_ids():
            _rc, _out, fail = _pactl("move-sink-input", si_id, EC_SINK_NAME)
            if fail is None:
                moved_sink_inputs.append(si_id)
            else:
                failed_sink_input_moves.append(si_id)

        _module_id = module_id
        _master_source = master_source
        _master_sink = master_sink
        _moved_source_outputs = moved_source_outputs
        _moved_sink_inputs = moved_sink_inputs
        _watch_chunks_remaining = _WATCH_CHUNKS
        _consec_silent_chunks = 0
        _fallback_started = False
        _active = True
        logger.info(
            "echo cancel engaged",
            module_id=module_id,
            source_master=master_source,
            sink_master=master_sink,
            moved_source_outputs=moved_source_outputs,
            moved_sink_inputs=moved_sink_inputs,
            failed_sink_input_moves=failed_sink_input_moves,
            engage_ms=round((time.monotonic() - t0) * 1000, 1),
            trigger=trigger,
        )


def _fail_locked(failure_class: str, trigger: str, t0: float) -> None:
    """Record an engage failure (caller holds ``_lock`` and has already
    rolled back any partial wiring). Sticky by design — no retry storm
    against a PA daemon that can't do EC; next node restart re-arms."""
    global _failed
    _failed = True
    logger.warning(
        "echo cancel engage FAILED — raw capture kept, layer "
        "sticky-disabled until node restart",
        failure_class=failure_class,
        engage_ms=round((time.monotonic() - t0) * 1000, 1),
        trigger=trigger,
    )


def disengage_for_music(trigger: str = "unspecified") -> None:
    """Unwire + unload the EC module. Never raises.

    Called on the self-playing True→False edge, and by the watchdog
    fallback thread. Idempotent — no-op when not engaged.
    """
    try:
        _disengage(trigger)
    except Exception as e:  # containment guarantee
        try:
            logger.warning(
                "echo cancel disengage crashed — module may be left "
                "loaded until startup normalization",
                error=str(e),
                trigger=trigger,
            )
        except Exception:
            pass


def _disengage(trigger: str) -> None:
    global _active, _module_id, _master_source, _master_sink
    global _moved_source_outputs, _moved_sink_inputs, _watch_chunks_remaining

    with _lock:
        if not _active:
            return
        t0 = time.monotonic()
        # Flip the flag FIRST so the watchdog and telemetry see
        # inactive immediately, then unwind.
        _active = False
        _watch_chunks_remaining = 0
        module_id = _module_id
        master_source = _master_source
        master_sink = _master_sink
        moved_source_outputs = _moved_source_outputs
        moved_sink_inputs = _moved_sink_inputs
        _module_id = None
        _master_source = None
        _master_sink = None
        _moved_source_outputs = []
        _moved_sink_inputs = []

        # Moves first (clean re-home), unload second. Stale ids (a
        # stream that closed mid-session) fail their pactl call
        # harmlessly; unloading the module re-homes anything missed —
        # PA moves orphaned streams to the defaults itself.
        restored_source_outputs: list[str] = []
        for so_id in moved_source_outputs:
            _rc, _out, fail = _pactl(
                "move-source-output", so_id, master_source or "@DEFAULT_SOURCE@",
            )
            if fail is None:
                restored_source_outputs.append(so_id)
        restored_sink_inputs: list[str] = []
        for si_id in moved_sink_inputs:
            _rc, _out, fail = _pactl(
                "move-sink-input", si_id, master_sink or "@DEFAULT_SINK@",
            )
            if fail is None:
                restored_sink_inputs.append(si_id)
        unloaded = _unload_module(module_id) if module_id else False
        logger.info(
            "echo cancel disengaged",
            module_id=module_id,
            unloaded=unloaded,
            restored_source_outputs=restored_source_outputs,
            restored_sink_inputs=restored_sink_inputs,
            disengage_ms=round((time.monotonic() - t0) * 1000, 1),
            trigger=trigger,
        )


# ── Silent-capture watchdog (auto-fallback) ────────────────────────────


def note_capture_chunk(rms: float) -> None:
    """Wake-loop per-chunk feed: detect capture death right after engage.

    Fed the raw-capture RMS the wake loop already computes per 80 ms
    chunk (the cheapest existing signal — no new pactl traffic, no new
    audio taps). If the first ~10 s of scored audio after an engage
    contain ≥ 2 s of consecutive near-zero chunks, the EC source is
    presumed wedged: trigger the fallback (background-thread disengage
    + sticky ``ec_failed``). Lock-free hot path; never raises.
    """
    global _watch_chunks_remaining, _consec_silent_chunks, _fallback_started
    if not _active or _watch_chunks_remaining <= 0:
        return
    try:
        _watch_chunks_remaining -= 1
        if rms < _SILENT_RMS:
            _consec_silent_chunks += 1
            if (
                _consec_silent_chunks >= _SILENT_CHUNKS_LIMIT
                and not _fallback_started
            ):
                _fallback_started = True
                _start_fallback("silent_capture_after_engage")
        else:
            _consec_silent_chunks = 0
    except Exception:
        pass


def _start_fallback(failure_class: str) -> None:
    """Sticky-fail + disengage on a daemon thread.

    The disengage does up to ~5 pactl round-trips (2 s timeout each);
    running it inline would stall wake scoring — the June-2026 8 s
    wake-stall lesson. The wake loop keeps consuming the (dead) EC
    source for the sub-second it takes the thread to move the capture
    back to the raw source.
    """
    global _failed
    _failed = True
    logger.warning(
        "echo cancel FALLBACK — capture silent after engage; disengaging "
        "to raw capture (sticky ec_failed until node restart)",
        failure_class=failure_class,
    )
    threading.Thread(
        target=disengage_for_music,
        kwargs={"trigger": f"fallback:{failure_class}"},
        name="jarvis-ec-fallback",
        daemon=True,
    ).start()


# ── Boot normalization ─────────────────────────────────────────────────


def normalize_on_startup() -> None:
    """Unload EC modules a crashed prior run left in the PA daemon.

    The PA daemon outlives the node process, so a crash while engaged
    strands ``module-echo-cancel`` (and its CPU cost) forever. Only
    rows positively identified as OURS — module name AND our unique
    ``source_name=jarvis_ec_source`` in the args — are touched;
    bluetooth's module-loopback instances and any foreign EC module
    are left alone. No stream moves needed: our capture stream doesn't
    exist yet at boot, and PA re-homes orphaned streams to the
    defaults when the module unloads. Never raises.
    """
    try:
        _rc, out, fail = _pactl("list", "modules", "short")
        if fail is not None:
            return
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or parts[1] != _EC_MODULE_NAME:
                continue
            args = parts[2] if len(parts) > 2 else ""
            if f"source_name={EC_SOURCE_NAME}" not in args:
                continue
            module_id = parts[0].strip()
            if not module_id:
                continue
            if _unload_module(module_id):
                logger.warning(
                    "stale jarvis echo-cancel module unloaded at boot "
                    "(prior run likely crashed while engaged)",
                    module_id=module_id,
                )
            else:
                logger.warning(
                    "stale jarvis echo-cancel module unload FAILED at boot",
                    module_id=module_id,
                )
    except Exception as e:
        try:
            logger.warning(
                "echo cancel startup normalization crashed", error=str(e),
            )
        except Exception:
            pass


def _reset_state_for_tests() -> None:
    """Test-only: return every module-level flag to boot state."""
    global _active, _failed, _module_id, _master_source, _master_sink
    global _moved_source_outputs, _moved_sink_inputs
    global _watch_chunks_remaining, _consec_silent_chunks, _fallback_started
    with _lock:
        _active = False
        _failed = False
        _module_id = None
        _master_source = None
        _master_sink = None
        _moved_source_outputs = []
        _moved_sink_inputs = []
        _watch_chunks_remaining = 0
        _consec_silent_chunks = 0
        _fallback_started = False
