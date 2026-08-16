"""Music-state detection + ducking (pause / resume) on the local box.

Two separate concerns share this module:

  * **Ducking** (temporary, wake-window only) — :func:`pause_active_playback`
    and :func:`resume_active_playback`. Mute or SIGSTOP players during voice
    capture; restore them after.

  * **Takeover** (permanent, on new "play") — :func:`stop_other_music_players`.
    When a music command starts new playback (Audacy plays radio while Spotify
    is mid-track, etc.), stop the others entirely so two streams don't share
    the speaker. Called from the command-execution service before any
    "play"-action command runs.

When a wake word fires we pause any active media-player so it doesn't
compete with the user's voice. Two classes of player get different
treatment because of process / protocol constraints:

  * **SIGSTOP-bound** (mpv, ffplay, cvlc, vlc, spotifyd, librespot):
    move the PA sink-input to a null sink AND SIGSTOP the process.
    The move prevents underrun-induced sink wedge — a frozen process
    can't supply samples to its uncorked sink-input → PA wedges the
    real sink in SUSPENDED. The null sink absorbs whatever PA pulls.

  * **Mute-only** (shairport-sync, go-librespot): leave the sink-input
    on the real sink and duck its volume to ``music_duck_percent``
    (default 20%; 0 = legacy full mute). Moving these to null pollutes PA's
    module-stream-restore database (which remembers per-app sink
    preference) — the NEXT sink-input the app creates would land on
    the null sink even after restore. These players also can't be
    SIGSTOP'd safely: shairport's RTSP TEARDOWN hangs; go-librespot's
    localhost HTTP API became unresponsive for 15+ s after SIGCONT
    (observed 2026-06-03), breaking the deferred-play hook.

The state query :func:`is_playing` sits alongside the ducking actions
in this module because the duck path consults it at the same point the
wake loop fires.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import urllib.error
import urllib.request

from jarvis_log_client import JarvisLogger

from utils.config_service import Config


logger = JarvisLogger(service="jarvis-node")


# Binaries that can be safely SIGSTOP'd while ducking — unidirectional
# consumers that read from an upstream source and write to PA. Pausing
# the process halts both ends without breaking any local protocol.
_SIGSTOP_PLAYER_BINARIES: tuple[str, ...] = (
    "mpv", "ffplay", "cvlc", "vlc",
    "spotifyd",      # jarvis-cmd-spotify (pre-v0.1.3, kept for backwards
                     # compat — pkill of a missing binary is a no-op)
    "librespot",     # jarvis-cmd-spotify v0.1.3–v1.x (apt-installed via
                     # the raspotify package)
)

# Binaries that must NOT be SIGSTOP'd because they participate in a
# request/response protocol with a remote peer expecting timely ACKs.
# Muting the PA sink-input is sufficient — the process keeps running
# and answering its protocol; audio reaches a muted sink during voice.
_MUTE_ONLY_PLAYER_BINARIES: tuple[str, ...] = (
    "shairport-sync",  # jarvis-cmd-music-assistant (AirPlay receiver)
    "go-librespot",    # jarvis-cmd-spotify v2.x+ — controlled via
                       # localhost HTTP API; SIGSTOP makes that API hang
                       # for 15+ seconds after SIGCONT, breaking the
                       # deferred-play hook the spotify command uses.
    "mpd",             # jarvis-cmd-audacy — mpd keeps a long-lived HTTP
                       # stream open to Audacy's CDN. SIGSTOP'ing it would
                       # freeze the read side, fill the kernel TCP buffer,
                       # and on SIGCONT resume the user from N-seconds-
                       # behind buffered audio (wrong for *live* radio:
                       # the user wants to be back at "now," not
                       # 8 seconds behind). Mute-only keeps mpd reading
                       # at real-time and produces a clean unmute.
)

# Union — used by the PA sink-input matcher, which mutes everything
# regardless of pause mechanism.
_PLAYER_BINARIES: tuple[str, ...] = (
    _SIGSTOP_PLAYER_BINARIES + _MUTE_ONLY_PLAYER_BINARIES
)

# Dedicated null sink for parking player sink-inputs during wake. Moving
# them off the real ALSA sink (instead of merely muting) means pulse no
# longer expects samples from them — without this, an uncorked-but-frozen
# sink-input causes underrun on the real sink, and after enough seconds
# pulse wedges the sink in a SUSPENDED state that can only be recovered
# by reloading module-alsa-card. Moving to null eliminates the underrun.
_DUCK_NULL_SINK_NAME = "jarvis_duck_null"

# Saved sink-input volumes for the volume-duck path (mute-only class):
# {sink_input_id: original_volume_pct}, captured at duck time and restored
# by :func:`resume_active_playback`. This is the ONE piece of module state
# the "deliberately stateless" note in pause_active_playback tolerates,
# because it has a hard lifecycle guarantee: it is populated only inside
# pause_active_playback and cleared unconditionally (pop-before-restore,
# so even a raising restore path clears it) inside the finally-driven
# resume_active_playback of the same wake cycle. The idempotence guard —
# an id already present is NOT re-read — matters because the duck fires
# multiple times per turn (wake-fire submit + per-follow-up-iteration
# re-duck in follow_up_loop): re-reading on the second fire would capture
# the already-ducked value and both double-duck and clobber the original.
#
# Why save originals at all instead of restoring a flat 100%:
# go-librespot (Spotify Connect) and shairport-sync (AirPlay) map the
# phone's volume slider onto the PA sink-input volume — restoring 100%
# would clobber the user's chosen level.
_saved_sink_input_volumes: dict[str, int] = {}
_duck_state_lock = threading.Lock()

# Cached "the node believes its own music is playing" flag (slice 2 of
# the wake-during-music work). Refreshed for free from the sink-input
# enumeration the duck already performs at every wake fire, and set True
# by the wake loop's deferred-play hook when a media command starts
# playback. Deliberately NO low-frequency background poll: pactl
# round-trips are not free on the Pi Zero and the flag only needs to be
# right at wake time. Known caveat: an EXTERNALLY-initiated session
# (e.g. the user AirPlays to the node from a phone while the node is
# idle) is not seen until the next wake fire's duck enumeration runs —
# so the very first wake into such a session reads a stale False. The
# fields this flag feeds are evidence/hints, never gates, so a stale
# read degrades to today's behavior rather than suppressing anything.
_self_playing: bool = False


def is_self_playing() -> bool:
    """Cheap cached read of "is the node's own music playing?".

    See the ``_self_playing`` comment for freshness semantics (refreshed
    at wake-fire duck time + deferred-play; externally-initiated
    sessions lag one wake).
    """
    return _self_playing


def set_self_playing(value: bool) -> None:
    """Set the cached self-playing flag.

    Called by the wake loop's deferred-play hook (a media command just
    started playback) and by the duck-time refresh.
    """
    global _self_playing
    _self_playing = bool(value)


def _duck_percent() -> int:
    """Duck depth for the mute-only player class, as a PA volume percent.

    ``music_duck_percent`` follows the same Config access pattern as the
    other voice-loop tunables (``silence_threshold_auto`` etc.). 0 (or
    negative) selects the legacy full-mute behavior.
    """
    return Config.get_int("music_duck_percent", 20)


def _list_sink_inputs() -> list[dict]:
    """One ``pactl -f json list sink-inputs`` round-trip, parsed.

    Shared by the state query, the id filters, and the duck path (which
    also reuses the same enumeration to refresh the self-playing flag —
    one subprocess per duck, not three). Fail-open: any subprocess or
    parse failure returns an empty list.
    """
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    try:
        items = json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        return []
    return items if isinstance(items, list) else []


def _item_binary_basename(item: dict) -> str:
    """Basename of the sink-input's producing process binary.

    PA reports the absolute path on Linux (e.g.
    /home/pi/.jarvis/spotify/bin/go-librespot) — match on basename so
    custom-install locations don't slip past the filter.
    """
    props = item.get("properties") or {}
    return os.path.basename(props.get("application.process.binary") or "")


def _items_for(items: list[dict], binaries: tuple[str, ...]) -> list[dict]:
    """Filter parsed sink-inputs to those produced by ``binaries``."""
    return [i for i in items if _item_binary_basename(i) in binaries]


def _volume_percent_from_item(item: dict) -> int | None:
    """Extract the sink-input's volume as an int percent from pactl JSON.

    pactl reports per-channel dicts (``{"front-left": {"value_percent":
    "61%", ...}, ...}`` or ``{"mono": {...}}``); we take the max across
    channels. Returns None when the shape is unrecognized — callers fall
    back to the legacy mute for that sink-input, which the unconditional
    unmute on restore always recovers from.
    """
    volume = item.get("volume")
    if not isinstance(volume, dict):
        return None
    percents: list[int] = []
    for channel in volume.values():
        if not isinstance(channel, dict):
            continue
        raw = channel.get("value_percent")
        if isinstance(raw, str) and raw.endswith("%"):
            try:
                percents.append(int(raw.rstrip("%").strip()))
            except ValueError:
                continue
    return max(percents) if percents else None


def is_playing() -> bool:
    """True if any tracked media-player has an UNCORKED PulseAudio sink-input.

    Process existence alone is misleading: spotifyd runs as a daemon 24/7
    listening for Spotify Connect commands, regardless of whether music
    is actually playing. The reliable signal is PA's cork state — a
    sink-input is uncorked iff the application is actively producing
    audio (including when we've SIGSTOP'd the process; the cork stays
    in the same state until SIGCONT). Falls back to False on any pactl
    failure rather than raising the wake threshold unnecessarily.
    """
    return _any_player_uncorked(_list_sink_inputs())


def _any_player_uncorked(items: list[dict]) -> bool:
    """The :func:`is_playing` predicate over an already-parsed listing."""
    return any(
        _item_binary_basename(item) in _PLAYER_BINARIES
        and not item.get("corked", True)
        for item in items
    )


def player_sink_input_ids() -> list[str]:
    """Return PA sink-input ids belonging to known media-player processes.

    SIGSTOP'ing the player only stops *new* audio production; up to
    several seconds of audio may already be buffered in PA's sink-input.
    To silence the speaker immediately we mute those sink-inputs
    directly. pactl reports ``application.process.binary`` for each
    sink-input — we match by basename.
    """
    return player_sink_input_ids_for(_PLAYER_BINARIES)


def player_sink_input_ids_for(binaries: tuple[str, ...]) -> list[str]:
    """Return PA sink-input ids whose process binary basename matches.

    Mirrors :func:`player_sink_input_ids` but takes a subset filter so
    the duck logic can apply different actions to different player
    classes (move-to-null for SIGSTOP-bound players to prevent
    underrun-induced sink wedge; mute-only for protocol-sensitive
    players whose HTTP/RTSP responsiveness would be broken by SIGSTOP).
    """
    ids: list[str] = []
    for item in _items_for(_list_sink_inputs(), binaries):
        sid = item.get("index")
        if sid is not None:
            ids.append(str(sid))
    return ids


def ensure_duck_null_sink() -> None:
    """Idempotently create the duck null sink used by
    :func:`pause_active_playback`."""
    try:
        r = subprocess.run(
            ["pactl", "list", "short", "sinks"],
            timeout=2.0, capture_output=True, text=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return
    if r.returncode != 0:
        return
    if any(_DUCK_NULL_SINK_NAME in line for line in r.stdout.splitlines()):
        return
    try:
        subprocess.run(
            [
                "pactl", "load-module", "module-null-sink",
                f"sink_name={_DUCK_NULL_SINK_NAME}",
                "sink_properties=device.description=jarvis-duck-null",
            ],
            timeout=2.0, capture_output=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


def pause_active_playback() -> None:
    """Silence (or duck) any active media-player subprocesses immediately.

    See module docstring for the two-class treatment. The SIGSTOP class
    is fully paused (null-sink park + SIGSTOP) exactly as before. The
    mute-only class is DUCKED to ``music_duck_percent`` (default 20%)
    instead of hard-muted, so the room keeps low-level music continuity
    through the exchange; setting it to 0 restores the legacy full-mute
    behavior. Original volumes are saved for the restore — see the
    ``_saved_sink_input_volumes`` comment for the lifecycle and the
    multi-fire idempotence guard.

    No internal "is-paused" flag: a previous version tracked state in a
    global so overlapping wake events wouldn't double-pause, but the
    flag drifted out of sync when wake events landed without any player
    running, then stuck at True. pkill/pactl against missing targets is
    harmless. (The saved-volume dict is not that flag reborn — it never
    gates whether the duck runs, it only remembers what to restore, and
    it is cleared unconditionally on every restore.)
    """
    ensure_duck_null_sink()
    items = _list_sink_inputs()
    # Slice-2 freshness for free: this enumeration is the same data the
    # cached self-playing flag needs, so refresh it here instead of
    # spending a second pactl round-trip anywhere else.
    set_self_playing(_any_player_uncorked(items))
    parked: list[str] = []
    for item in _items_for(items, _SIGSTOP_PLAYER_BINARIES):
        if item.get("index") is None:
            continue
        sink_input_id = str(item.get("index"))
        try:
            r = subprocess.run(
                ["pactl", "move-sink-input", sink_input_id, _DUCK_NULL_SINK_NAME],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                parked.append(sink_input_id)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    duck_pct = _duck_percent()
    muted: list[str] = []
    ducked: list[str] = []
    for item in _items_for(items, _MUTE_ONLY_PLAYER_BINARIES):
        if item.get("index") is None:
            continue
        sink_input_id = str(item.get("index"))
        original_pct: int | None = None
        if duck_pct > 0:
            with _duck_state_lock:
                if sink_input_id in _saved_sink_input_volumes:
                    # Idempotence guard: the duck fires N times per turn
                    # (wake fire + each follow-up iteration). A repeat
                    # fire must NOT re-read the volume — it would capture
                    # the ducked value, then "restore" music to 20%.
                    original_pct = _saved_sink_input_volumes[sink_input_id]
                else:
                    original_pct = _volume_percent_from_item(item)
                    if original_pct is not None:
                        _saved_sink_input_volumes[sink_input_id] = original_pct
        if duck_pct > 0 and original_pct is not None:
            try:
                r = subprocess.run(
                    ["pactl", "set-sink-input-volume", sink_input_id,
                     f"{duck_pct}%"],
                    timeout=2.0, capture_output=True,
                )
                if r.returncode == 0:
                    ducked.append(sink_input_id)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
        else:
            # Legacy full-mute path: duck depth 0 (explicit opt-out) or
            # an unreadable original volume (ducking without a saved
            # original would strand the stream at duck level — the
            # unconditional unmute on restore always recovers a mute).
            try:
                r = subprocess.run(
                    ["pactl", "set-sink-input-mute", sink_input_id, "1"],
                    timeout=2.0, capture_output=True,
                )
                if r.returncode == 0:
                    muted.append(sink_input_id)
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
                pass
    stopped: list[str] = []
    for binary in _SIGSTOP_PLAYER_BINARIES:
        try:
            r = subprocess.run(
                ["pkill", "-STOP", "-x", binary],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                stopped.append(binary)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    logger.info(
        "pause_active_playback",
        parked_sink_inputs=parked,
        muted_sink_inputs=muted,
        ducked_sink_inputs=ducked,
        duck_percent=duck_pct,
        sigstopped=stopped,
        self_playing=is_self_playing(),
    )


def resume_active_playback() -> None:
    """Reverse the duck: SIGCONT first, then move parked / unmute muted."""
    # SIGCONT BEFORE moves/unmutes — pulse buffers anything the resumed
    # process writes regardless of where the sink-input lives, and the
    # ordering keeps the resume-audio gap small.
    resumed: list[str] = []
    for binary in _SIGSTOP_PLAYER_BINARIES:
        try:
            r = subprocess.run(
                ["pkill", "-CONT", "-x", binary],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                resumed.append(binary)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    restored: list[str] = []
    for sink_input_id in player_sink_input_ids_for(_SIGSTOP_PLAYER_BINARIES):
        try:
            r = subprocess.run(
                ["pactl", "move-sink-input", sink_input_id, "@DEFAULT_SINK@"],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                restored.append(sink_input_id)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    unmuted: list[str] = []
    for sink_input_id in player_sink_input_ids_for(_MUTE_ONLY_PLAYER_BINARIES):
        try:
            r = subprocess.run(
                ["pactl", "set-sink-input-mute", sink_input_id, "0"],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                unmuted.append(sink_input_id)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    # Volume-duck restore: put every saved original volume back. The dict
    # is drained UP FRONT (pop-all under the lock) so it is cleared on
    # every path through here — including a raising pactl — and a
    # concurrent duck can never observe half-restored state as "already
    # saved". A sink-input that vanished mid-turn just fails its pactl
    # call harmlessly; the entry is gone from the dict either way.
    with _duck_state_lock:
        saved_volumes = dict(_saved_sink_input_volumes)
        _saved_sink_input_volumes.clear()
    unducked: list[str] = []
    for sink_input_id, original_pct in saved_volumes.items():
        try:
            r = subprocess.run(
                ["pactl", "set-sink-input-volume", sink_input_id,
                 f"{original_pct}%"],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                unducked.append(sink_input_id)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    logger.info(
        "resume_active_playback",
        sigcont=resumed,
        restored_sink_inputs=restored,
        unmuted_sink_inputs=unmuted,
        unducked_sink_inputs=unducked,
        restored_volumes=saved_volumes,
    )


# ───────────────────────────────────────────────────────────────────────
# Takeover: permanent stop of other music players when a new play starts
# ───────────────────────────────────────────────────────────────────────

# Existing music packages that pre-date the SDK property are routed via
# this map. New music packages should declare a `takes_over_playback_binary`
# property on their command class instead — :func:`_resolve_takeover_binary`
# prefers the property and falls back to this map.
_LEGACY_MUSIC_TAKEOVER_BINARIES: dict[str, str] = {
    "audacy":  "mpd",
    "spotify": "go-librespot",
    "music":   "shairport-sync",    # jarvis-cmd-music-assistant
    "pandora": "mpv",
}


def _resolve_takeover_binary(command: object) -> str | None:
    """Pick the binary a command controls so we don't stop ourselves.

    Property wins over the legacy map — packages opt in by declaring
    ``takes_over_playback_binary`` on their command class.
    """
    declared = getattr(command, "takes_over_playback_binary", None)
    if isinstance(declared, str) and declared:
        return declared
    name = getattr(command, "command_name", None)
    if isinstance(name, str):
        return _LEGACY_MUSIC_TAKEOVER_BINARIES.get(name)
    return None


def _stop_mpd_via_tcp() -> None:
    """Send `stop` + `clear` to mpd's TCP control port.

    Tiny stdlib protocol speaker — mpd's wire format is text-over-socket,
    we don't need python-mpd2 just to send two commands. Best-effort; a
    swallowed OSError means mpd wasn't running, which is the takeover's
    desired end state anyway.
    """
    try:
        with socket.create_connection(("localhost", 6600), timeout=2.0) as s:
            s.settimeout(2.0)
            # Consume the "OK MPD <version>\n" greeting.
            _greeting = s.recv(1024)
            s.sendall(b"command_list_begin\nstop\nclear\ncommand_list_end\n")
            # Drain the OK/ACK response so the close is clean.
            _ = s.recv(1024)
    except (OSError, socket.timeout):
        pass


def _stop_go_librespot_via_http() -> None:
    """POST to go-librespot's local pause endpoint.

    stdlib urllib so we don't need an httpx dep in node core — go-librespot
    binds to localhost:3678 by default (jarvis-cmd-spotify v2.x+).
    """
    try:
        req = urllib.request.Request(
            "http://localhost:3678/player/pause", method="POST",
        )
        urllib.request.urlopen(req, timeout=2.0).close()
    except (urllib.error.URLError, OSError, TimeoutError):
        pass


def _pkill_int(binary: str) -> None:
    """SIGINT a player binary. Matches by exact basename (`pkill -x`)."""
    try:
        subprocess.run(
            ["pkill", "-INT", "-x", binary],
            timeout=2.0, capture_output=True,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass


# Players whose stop strategy is SIGINT. Other strategies (mpd TCP,
# go-librespot HTTP) are dispatched inline by name in :func:`stop_other_music_players`
# — name lookup happens at call time so monkeypatch/replacement works
# (a dict of direct function references would freeze the originals at
# import time and silently bypass any later override).
_PKILL_INT_BINARIES: frozenset[str] = frozenset({
    "mpv", "ffplay", "vlc", "cvlc",
    "spotifyd", "librespot", "shairport-sync",
})


def _active_player_binaries() -> set[str]:
    """Snapshot the set of player binaries currently producing audio.

    Uses the same pactl sink-input scan as :func:`is_playing` but returns
    every matched binary basename — we need the full set so the takeover
    helper knows which strategies to invoke.
    """
    return {
        _item_binary_basename(item)
        for item in _items_for(_list_sink_inputs(), _PLAYER_BINARIES)
    }


def stop_other_music_players(except_binary: str | None = None) -> None:
    """Permanently stop every music-producing player except the named one.

    Called by the command-execution service right before a music command's
    "play" action runs, so the new playback takes over the speaker cleanly
    instead of layering on top of whatever was already playing.

    :param except_binary: The binary the calling command controls — left
        alone so re-tuning (e.g. switching Audacy stations) doesn't stop
        the in-flight player. ``None`` stops everything.

    Best-effort and idempotent: per-binary strategy failures are logged
    but never raised. Players that aren't currently producing audio are
    skipped — we don't want to thrash a paused but live daemon.
    """
    active = _active_player_binaries()
    if not active:
        return
    stopped: list[str] = []
    skipped: list[str] = []
    for binary in active:
        if binary == except_binary:
            skipped.append(binary)
            continue
        try:
            # Dispatch inline so name lookup is at call time — monkeypatch /
            # future strategy replacement Just Works without rebuilding a
            # cached dict.
            if binary == "mpd":
                _stop_mpd_via_tcp()
            elif binary == "go-librespot":
                _stop_go_librespot_via_http()
            elif binary in _PKILL_INT_BINARIES:
                _pkill_int(binary)
            else:
                # Active player we don't know how to stop — log and move on
                # rather than risk a wrong-shaped pkill against an unfamiliar
                # process.
                logger.warning(
                    "stop_other_music_players: no stop strategy",
                    binary=binary,
                )
                continue
            stopped.append(binary)
        except Exception as e:  # belt-and-suspenders; strategies already swallow
            logger.warning(
                "stop_other_music_players: strategy raised",
                binary=binary, error=str(e),
            )
    logger.info(
        "stop_other_music_players",
        except_binary=except_binary,
        stopped=stopped,
        skipped=skipped,
    )
