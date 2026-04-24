"""Resolve the PyAudio input device index for microphone capture.

Centralized because the wake-word listener and the command listener
need to agree on the same physical device. If they disagree, the wake
stream captures real speech while the command stream opens a different
device that captures ambient silence — whisper returns [BLANK_AUDIO]
for every command.

Name-based matching is preferred over a numeric index: ALSA card order
on Raspberry Pi isn't stable across boots when HDMI, hifiberry, and a
USB mic race on enumeration.
"""

from __future__ import annotations

import pyaudio

from jarvis_log_client import JarvisLogger
from utils.config_service import Config

logger = JarvisLogger(service="jarvis-node")


def resolve_input_device_index(
    pa_instance: "pyaudio.PyAudio | None" = None,
) -> int | None:
    """Pick an input device index at stream-open time.

    Resolution order:
      1. Substring match against ``mic_device_name`` (most resilient).
      2. A configured ``mic_device_index`` if it points at an input
         device (rejected with a warning otherwise).
      3. First device with ``maxInputChannels > 0``.

    Returns None only when the system has no input devices at all — the
    caller should fall back to PyAudio's default in that case.

    If no ``pa_instance`` is supplied, a throwaway PyAudio instance is
    created and terminated for the resolution.
    """
    owns_pa = pa_instance is None
    if pa_instance is None:
        pa_instance = pyaudio.PyAudio()
    try:
        return _resolve_impl(pa_instance)
    finally:
        if owns_pa:
            pa_instance.terminate()


def _resolve_impl(pa_instance: "pyaudio.PyAudio") -> int | None:
    mic_device_name: str | None = Config.get_str("mic_device_name")
    mic_index_str: str | None = Config.get_str("mic_device_index")
    mic_device_index: int | None = (
        int(mic_index_str) if mic_index_str is not None else None
    )

    if mic_device_name:
        needle = mic_device_name.lower()
        for i in range(pa_instance.get_device_count()):
            info = pa_instance.get_device_info_by_index(i)
            if (int(info.get("maxInputChannels", 0) or 0) > 0
                    and needle in str(info.get("name", "")).lower()):
                logger.info(
                    "Mic resolved by name",
                    pattern=mic_device_name,
                    matched=info.get("name"),
                    index=i,
                )
                return i
        logger.warning(
            "mic_device_name set but no input device matched — falling back",
            pattern=mic_device_name,
        )

    if mic_device_index is not None:
        try:
            info = pa_instance.get_device_info_by_index(mic_device_index)
            if int(info.get("maxInputChannels", 0) or 0) > 0:
                return mic_device_index
            logger.warning(
                "mic_device_index points at an output-only device — falling back",
                index=mic_device_index,
                name=info.get("name"),
            )
        except (OSError, ValueError) as e:
            logger.warning(
                "mic_device_index invalid — falling back",
                index=mic_device_index,
                error=str(e),
            )

    for i in range(pa_instance.get_device_count()):
        info = pa_instance.get_device_info_by_index(i)
        if int(info.get("maxInputChannels", 0) or 0) > 0:
            logger.warning(
                "Auto-selected first input device (no mic_device_name/index configured)",
                name=info.get("name"),
                index=i,
            )
            return i

    logger.error("No input devices found on this system")
    return None
