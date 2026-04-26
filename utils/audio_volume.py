"""ALSA volume read/write via amixer.

Used by:
- mqtt_tts_listener.handle_update_node_config — when the mobile app
  pushes a `volume_percent` key, we shell out to amixer instead of
  writing the key to config.json (volume is OS-level, not app config).
- settings_snapshot_service.build_snapshot — includes the current
  volume in the snapshot so the mobile slider reflects reality.

Hardcodes the HiFiBerry DAC HAT (the only audio device used by Jarvis
nodes today). If/when other audio devices appear, expose
`alsa_mixer_card` and `alsa_mixer_control` as config keys and read them
here.
"""

import re
import subprocess

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

_CARD = "sndrpihifiberry"
_CONTROL = "SoftMaster"


def get_volume_percent() -> int | None:
    """Return current volume as 0-100, or None if amixer/control missing."""
    try:
        result = subprocess.run(
            ["amixer", "-c", _CARD, "sget", _CONTROL],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.debug("amixer get failed", error=str(e))
        return None
    if result.returncode != 0:
        return None
    # Output line we want:  Front Left: 255 [100%] [0.00dB]
    match = re.search(r"\[(\d+)%\]", result.stdout)
    if not match:
        return None
    return int(match.group(1))


def set_volume_percent(pct: int) -> bool:
    """Clamp to [0, 100] and apply via amixer. True on success."""
    pct = max(0, min(100, int(pct)))
    try:
        result = subprocess.run(
            ["amixer", "-c", _CARD, "sset", _CONTROL, f"{pct}%"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("amixer set failed", error=str(e))
        return False
    if result.returncode != 0:
        logger.warning("amixer set non-zero exit", stderr=result.stderr.strip())
        return False
    logger.info("Volume set", percent=pct)
    return True
