"""Concrete AudioControlBackend for the node runtime.

Bridges the SDK's ``audio_*`` helpers to ``utils.audio_volume`` (ALSA /
PulseAudio volume and mute) and to the node's TTS provider for ``speak``
(the same blocking ``get_tts_provider().speak(False, text)`` path the timer
and alert announcements use).

Registered once at startup via:
    from services.audio_control_backend import init_audio_control_backend
    init_audio_control_backend()
"""

from __future__ import annotations

from jarvis_command_sdk.audio_control import (
    AudioControlBackend,
    set_audio_control_backend,
)

from utils import audio_volume


class NodeAudioControlBackend(AudioControlBackend):
    """Audio-control backend backed by the node's mixer and TTS provider."""

    def set_volume_percent(self, percent: int) -> bool:
        return audio_volume.set_volume_percent(percent)

    def adjust_volume_percent(self, delta: int) -> int | None:
        return audio_volume.adjust_volume_percent(delta)

    def set_muted(self, muted: bool) -> bool:
        return audio_volume.set_muted(muted)

    def speak(self, text: str) -> bool:
        # Deferred: core.helpers pulls in the TTS providers and config.
        from core.helpers import get_tts_provider

        get_tts_provider().speak(False, text)
        return True


def init_audio_control_backend() -> None:
    """Register the node audio-control backend with the SDK. Call once at startup."""
    set_audio_control_backend(NodeAudioControlBackend())
