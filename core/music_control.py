"""Music-state detection + ducking (pause / resume) on the local box.

When a wake word fires we pause any active media-player so it doesn't
compete with the user's voice. Two classes of player get different
treatment because of process / protocol constraints:

  * **SIGSTOP-bound** (mpv, ffplay, cvlc, vlc, spotifyd, librespot):
    move the PA sink-input to a null sink AND SIGSTOP the process.
    The move prevents underrun-induced sink wedge — a frozen process
    can't supply samples to its uncorked sink-input → PA wedges the
    real sink in SUSPENDED. The null sink absorbs whatever PA pulls.

  * **Mute-only** (shairport-sync, go-librespot): leave the sink-input
    on the real sink and mute it. Moving these to null pollutes PA's
    module-stream-restore database (which remembers per-app sink
    preference) — the NEXT sink-input the app creates would land on
    the null sink even after restore. These players also can't be
    SIGSTOP'd safely: shairport's RTSP TEARDOWN hangs; go-librespot's
    localhost HTTP API became unresponsive for 15+ s after SIGCONT
    (observed 2026-06-03), breaking the deferred-play hook.

State queries (:func:`is_playing`, :func:`wake_music_energy_multiplier`)
sit alongside ducking actions in this module because the wake loop
consults both at the same point — "is music playing?" decides the
threshold and energy multiplier; the duck fires when the wake passes.
"""

from __future__ import annotations

import json
import os
import subprocess

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


def wake_music_energy_multiplier() -> float:
    """How far current RMS must rise above the running baseline to fire
    a wake during music playback.

    Music alone occupies a fairly stable RMS band — a voice spoken over
    it adds energy on top, producing a spike of ~1.5-2.5x the music
    baseline at a normal speaking distance from the Pi Zero mic.
    Tunable via the ``wake_word_music_energy_multiplier`` setting if
    the room's speaker bleed profile is unusual.
    """
    return Config.get_float("wake_word_music_energy_multiplier", 1.5)


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
    try:
        result = subprocess.run(
            ["pactl", "-f", "json", "list", "sink-inputs"],
            capture_output=True, text=True, timeout=2.0,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return False
    if result.returncode != 0:
        return False
    try:
        items = json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        return False
    for item in items:
        props = item.get("properties") or {}
        binary = props.get("application.process.binary") or ""
        # PA reports the absolute path on Linux (e.g.
        # /home/pi/.jarvis/spotify/bin/go-librespot) — match on basename
        # so custom-install locations don't slip past the filter.
        if (
            os.path.basename(binary) in _PLAYER_BINARIES
            and not item.get("corked", True)
        ):
            return True
    return False


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
    ids: list[str] = []
    for item in items:
        props = item.get("properties") or {}
        binary = props.get("application.process.binary") or ""
        if os.path.basename(binary) in binaries:
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
    """Silence any active media-player subprocesses immediately.

    See module docstring for the two-class treatment. No internal
    "is-paused" flag: a previous version tracked state in a global so
    overlapping wake events wouldn't double-pause, but the flag drifted
    out of sync when wake events landed without any player running,
    then stuck at True. pkill/pactl against missing targets is harmless.
    """
    ensure_duck_null_sink()
    parked: list[str] = []
    for sink_input_id in player_sink_input_ids_for(_SIGSTOP_PLAYER_BINARIES):
        try:
            r = subprocess.run(
                ["pactl", "move-sink-input", sink_input_id, _DUCK_NULL_SINK_NAME],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                parked.append(sink_input_id)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    muted: list[str] = []
    for sink_input_id in player_sink_input_ids_for(_MUTE_ONLY_PLAYER_BINARIES):
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
        sigstopped=stopped,
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
    logger.info(
        "resume_active_playback",
        sigcont=resumed,
        restored_sink_inputs=restored,
        unmuted_sink_inputs=unmuted,
    )
