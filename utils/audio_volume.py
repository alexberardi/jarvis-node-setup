"""PulseAudio-based volume / mute control for the ReSpeaker 2-Mics Pi HAT v2.

Used by:
- mqtt_tts_listener.handle_update_node_config — when the mobile app
  pushes a ``volume_percent`` or ``is_muted`` key.
- commands.control_node_command — voice intents ("volume up", "set
  volume to 5", "mute", ...).
- settings_snapshot_service.build_snapshot — current volume + detected
  audio card in the snapshot so the mobile slider and Hardware tab
  reflect reality.

All runtime control goes through PulseAudio (``pactl set-sink-volume`` /
``set-sink-mute``). The codec mixer baseline (TLV320 PCM at full, Line
at 0 dB, HP muted) is set once by install.sh — we never touch amixer
at runtime.

Why two sink classes (@DEFAULT_SINK@ + bluez_sink.*)?
@DEFAULT_SINK@ covers TTS, chimes, and music routed to the on-board
JST speaker. bluez_sink.* covers anything routed to a paired Bluetooth
speaker (PA exposes those as separate sinks). Setting both keeps every
audio path coherent regardless of where the user has pointed playback.
"""

import json
import os
import re
import subprocess

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


def _run(cmd: list[str], timeout: float = 2.0) -> subprocess.CompletedProcess | None:
    """Run a shell command, returning None when the binary is missing or times out."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug(f"{cmd[0]} unavailable", error=str(e))
        return None


# ── Audio card discovery (diagnostic only) ──────────────────────────────────

# Pi's built-in HDMI audio device — present on every Pi whether a screen
# is attached or not. The on-board speaker we care about is always the
# next non-HDMI card.
_HDMI_CARD_PREFIX = "vc4"


def get_audio_card() -> str | None:
    """Return the on-board audio card name (e.g. ``seeed2micvoicec``).

    Diagnostic-only — surfaces in the settings snapshot so the mobile
    Hardware tab can show what the node detected. Returns None when no
    non-HDMI card is present (e.g. macOS dev node, plain Pi with no HAT).
    """
    result = _run(["aplay", "-l"])
    if result is None or result.returncode != 0:
        return None
    for match in re.finditer(r"^card\s+\d+:\s+(\S+)", result.stdout, re.MULTILINE):
        name = match.group(1)
        if not name.startswith(_HDMI_CARD_PREFIX):
            return name
    return None


# ── Persistence (config.json) ───────────────────────────────────────────────


def _config_path() -> str:
    return os.path.expandvars(os.path.expanduser(
        os.environ.get("CONFIG_PATH", "config.json")
    ))


def _read_persisted_user_pct() -> int | None:
    """Return ``volume_percent`` from config.json, or None."""
    try:
        with open(_config_path()) as f:
            config = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    val = config.get("volume_percent")
    if val is None:
        return None
    try:
        return max(0, min(100, int(val)))
    except (TypeError, ValueError):
        return None


def persist_volume_percent(pct: int) -> bool:
    """Write ``volume_percent`` to config.json so the value survives reboot.

    Called automatically from ``set_volume_percent`` after a successful
    apply — callers don't usually invoke this directly.
    """
    pct = max(0, min(100, int(pct)))
    path = _config_path()
    try:
        try:
            with open(path) as f:
                config = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            config = {}
        config["volume_percent"] = pct
        with open(path, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except OSError as e:
        logger.warning("persist volume_percent failed", error=str(e))
        return False


# ── PulseAudio helpers ──────────────────────────────────────────────────────


def _list_bluez_sinks() -> list[str]:
    """Return PulseAudio sink names matching ``bluez_sink.*``. Empty if none."""
    result = _run(["pactl", "list", "short", "sinks"])
    if result is None or result.returncode != 0:
        return []
    sinks: list[str] = []
    for line in result.stdout.splitlines():
        cols = line.split("\t")
        if len(cols) >= 2 and cols[1].startswith("bluez_sink."):
            sinks.append(cols[1])
    return sinks


def _pactl_set_sink_volume(sink: str, pct: int) -> bool:
    result = _run(["pactl", "set-sink-volume", sink, f"{pct}%"])
    return result is not None and result.returncode == 0


def _pactl_set_sink_mute(sink: str, muted: bool) -> bool:
    result = _run(["pactl", "set-sink-mute", sink, "1" if muted else "0"])
    return result is not None and result.returncode == 0


def _pactl_get_default_sink_volume() -> int | None:
    """Parse the highest channel % from ``pactl get-sink-volume @DEFAULT_SINK@``."""
    result = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    if result is None or result.returncode != 0:
        return None
    # Example output: "Volume: front-left: 32768 /  50% / -18.00 dB,
    #                          front-right: 32768 /  50% / -18.00 dB"
    matches = re.findall(r"(\d+)%", result.stdout)
    if not matches:
        return None
    return max(int(m) for m in matches)


def _pactl_get_default_sink_mute() -> bool | None:
    """Return True/False from ``pactl get-sink-mute @DEFAULT_SINK@``, or None."""
    result = _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"])
    if result is None or result.returncode != 0:
        return None
    # Example output: "Mute: yes" or "Mute: no"
    line = result.stdout.strip().lower()
    if line.endswith("yes"):
        return True
    if line.endswith("no"):
        return False
    return None


# ── Public API ──────────────────────────────────────────────────────────────


def get_volume_percent() -> int | None:
    """Return current volume as 0-100, or None if unreadable.

    Prefers the persisted value in config.json so the contract is exact —
    ``set_volume_percent(70)`` → ``get_volume_percent()`` returns 70 (no
    drift through PulseAudio's integer percent). Falls back to the live
    PA reading when config.json has no volume_percent yet, so the mobile
    slider still has something to display on first boot.
    """
    persisted = _read_persisted_user_pct()
    if persisted is not None:
        return persisted
    return _pactl_get_default_sink_volume()


def is_muted() -> bool | None:
    """Return True if PA's default sink is muted, False if not, None if unreadable."""
    return _pactl_get_default_sink_mute()


def set_volume_percent(pct: int) -> bool:
    """Clamp to [0, 100] and apply across PA's default sink + every BT sink.

    Returns True if @DEFAULT_SINK@ was set; BT sinks are best-effort
    (no-op when none are paired).
    """
    pct = max(0, min(100, int(pct)))

    default_ok = _pactl_set_sink_volume("@DEFAULT_SINK@", pct)
    for sink in _list_bluez_sinks():
        _pactl_set_sink_volume(sink, pct)

    if default_ok:
        persist_volume_percent(pct)
        logger.info("Volume set", percent=pct)
    else:
        logger.warning("pactl set-sink-volume @DEFAULT_SINK@ failed")
    return default_ok


def adjust_volume_percent(delta: int) -> int | None:
    """Add ``delta`` percentage points, clamped to [0, 100]. None if unreadable."""
    current = get_volume_percent()
    if current is None:
        return None
    new = max(0, min(100, current + int(delta)))
    if not set_volume_percent(new):
        return None
    return new


def set_muted(muted: bool) -> bool:
    """Mute/unmute PA's default sink + every BT sink. True if default sink succeeded."""
    default_ok = _pactl_set_sink_mute("@DEFAULT_SINK@", muted)
    for sink in _list_bluez_sinks():
        _pactl_set_sink_mute(sink, muted)

    if default_ok:
        logger.info("Mute set", muted=muted)
    else:
        logger.warning("pactl set-sink-mute @DEFAULT_SINK@ failed")
    return default_ok
