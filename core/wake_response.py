"""Wake-acknowledgment audio + LED + warmup helpers.

When the wake word fires, this module decides what the user hears and
sees as confirmation, kicks off background warmup so the LLM has a
running start during the user's command, and pre-generates the next
cycle's audio so the next "Hey Jarvis" plays instantly.

Three audio paths in priority order, all driven by ``play_wake_ack``:

  1. **Cached LLM response** — a real first-person sentence that
     command-center pre-generated during the last interaction (e.g.
     "I'm here. What's up?"). Highest quality; only present when the
     previous turn actually finished and had time to fetch the next ack.
  2. **Bundled WAV chime** — a deterministic, latency-free fallback
     that ships with the node. Picked at random from ``sounds/wake/``.
  3. **Live TTS speak** — last-resort, hits the TTS provider with
     either the pre-fetched text in ``WAKE_FILE`` or a static "Yes?".

The module needs two runtime dependencies from the voice loop:

  * ``_bg_executor`` — the bounded ThreadPoolExecutor that owns
    fire-and-forget pre-generation work; bare ``threading.Thread()``
    leaked 1-2 orphans per voice command in earlier iterations.
  * ``_wake_paused`` factory — a context manager that pauses wake
    detection while our own response audio plays back. Without it the
    wake-word model hears our response and re-fires, producing 2x
    playback (~2.7 s of dead air for one "Hey Jarvis").

Both are injected via :func:`set_runtime` from voice_listener at
module init. The defaults (``None`` executor, no-op context manager)
keep this module importable in tests without the full audio stack.
"""

from __future__ import annotations

import io
import os
import random
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING, Callable, ContextManager

import numpy as np
from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from core.helpers import get_tts_provider, get_wake_response_provider
from core.platform_audio import platform_audio
from utils.config_service import Config
from utils.encryption_utils import get_cache_dir
from utils.service_discovery import get_command_center_url

if TYPE_CHECKING:
    from utils.command_execution_service import CommandExecutionService


logger = JarvisLogger(service="jarvis-node")


CHIME_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "sounds", "chime.wav"
)
_cache_dir = get_cache_dir()
WAKE_FILE = _cache_dir / "next_wake_response.txt"
WAKE_AUDIO_FILE = _cache_dir / "next_wake_response.wav"
PROCESSING_ACK_FILE = _cache_dir / "next_processing_ack.wav"

# Short, snappy acks played immediately after recording ends to fill the
# dead air while STT + LLM process. No LLM needed — just variety.
_PROCESSING_ACK_POOL: list[str] = [
    "One moment.",
    "Got it.",
    "Working on it.",
    "Let me check.",
    "On it.",
    "Give me a second.",
]

_WAKE_CHIMES_DIR = Path(__file__).resolve().parent.parent / "sounds" / "wake"


# Runtime deps injected by voice_listener at module init. Defaults are
# safe no-ops so tests can import this module without the full audio
# stack; the executor stays None and submit() calls become attribute
# errors only if the test actually triggers them (and tests that do
# install a fake executor first).
_bg_executor: ThreadPoolExecutor | None = None
_wake_paused: Callable[[], ContextManager] = nullcontext


def set_runtime(
    *,
    bg_executor: ThreadPoolExecutor,
    wake_paused_factory: Callable[[], ContextManager],
) -> None:
    """Inject the runtime deps from voice_listener.

    Called once at voice_listener module init. Tests don't call this
    — they monkeypatch the module attributes directly with fakes.
    """
    global _bg_executor, _wake_paused
    _bg_executor = bg_executor
    _wake_paused = wake_paused_factory


def run_warmup(
    command_service: "CommandExecutionService",
    conversation_id: str,
    speaker_user_id: int | None,
    speaker_confidence: float | None,
    result: dict,
) -> None:
    """Run conversation warmup in a background thread (during recording).

    Populates ``result["success"]`` so the caller can check whether the
    warmup succeeded after joining the thread.
    """
    try:
        success = command_service.register_tools_for_conversation(
            conversation_id,
            speaker_user_id=speaker_user_id,
            speaker_confidence=speaker_confidence,
        )
        result["success"] = success
    except Exception as e:
        logger.warning("Background warmup failed", error=str(e))
        result["success"] = False


def _bundled_wake_chimes() -> list[Path]:
    """List the pre-generated wake chime WAVs bundled with the node."""
    if not _WAKE_CHIMES_DIR.exists():
        return []
    return sorted(_WAKE_CHIMES_DIR.glob("*.wav"))


def set_led_transient(pattern: str | None) -> None:
    """Best-effort LED transient state change. Silent on any failure."""
    try:
        from services.led_service import get_led_service
        get_led_service().set_transient_pattern(pattern)
    except Exception:
        pass


def play_wake_ack():
    """Play the wake acknowledgment audio (cached LLM > bundled WAV > TTS).

    Extracted so it can be called either immediately at wake-time (legacy
    behavior) or deferred to fire only on the LLM-fallback path (when
    `wake_ack_audio_enabled` is False, the immediate playback is skipped
    and this is invoked later by `process_voice_command`).
    """
    t_enter = time.perf_counter()
    played = False
    source = "none"
    file_size = 0

    if WAKE_AUDIO_FILE.exists():
        source = "cached_llm"
        try:
            file_size = WAKE_AUDIO_FILE.stat().st_size
        except OSError:
            pass
        t_pre = time.perf_counter()
        # Pause wake detection while we play the wake response — otherwise
        # the wake-word model can hear our own response audio and retrigger,
        # causing 2x playback (~2.7s of dead air for one "Hey Jarvis").
        try:
            with _wake_paused():
                played = platform_audio.play_audio_file(str(WAKE_AUDIO_FILE))
        except Exception as e:
            logger.warning("Failed to play cached wake audio", error=str(e))
        finally:
            t_post = time.perf_counter()
            WAKE_AUDIO_FILE.unlink(missing_ok=True)
            WAKE_FILE.unlink(missing_ok=True)
            logger.info(
                f"wake audio timing | source={source} size={file_size}B "
                f"pre={int((t_pre - t_enter) * 1000)}ms "
                f"play={int((t_post - t_pre) * 1000)}ms "
                f"total={int((t_post - t_enter) * 1000)}ms"
            )

    if not played:
        bundled = _bundled_wake_chimes()
        if bundled:
            chime = random.choice(bundled)
            source = "bundled"
            try:
                file_size = chime.stat().st_size
            except OSError:
                pass
            t_pre = time.perf_counter()
            try:
                with _wake_paused():
                    played = platform_audio.play_audio_file(str(chime))
                if played:
                    logger.debug("Played bundled wake chime", chime=chime.name)
            except Exception as e:
                logger.warning("Failed to play bundled wake chime", chime=chime.name, error=str(e))
            t_post = time.perf_counter()
            logger.info(
                f"wake audio timing | source={source} size={file_size}B "
                f"pre={int((t_pre - t_enter) * 1000)}ms "
                f"play={int((t_post - t_pre) * 1000)}ms "
                f"total={int((t_post - t_enter) * 1000)}ms"
            )

    if not played:
        tts_provider = get_tts_provider()
        if WAKE_FILE.exists():
            wake_text = WAKE_FILE.read_text().strip()
            WAKE_FILE.unlink(missing_ok=True)
        else:
            wake_text = "Yes?"
        tts_provider.speak(False, wake_text)


def handle_keyword_detected():
    logger.info("Wake word detected, listening for command")
    print("Wake word detected! Listening...")
    set_led_transient("wake_detected")

    # Audio ack is optional. When wake_ack_audio_enabled=False, the LED
    # alone signals "I heard you" at wake-time, and the wake-ack audio is
    # deferred until we know we're going to hit the LLM (so it covers the
    # slow path's latency instead of front-loading dead air on fast paths).
    if Config.get_bool("wake_ack_audio_enabled", True):
        play_wake_ack()
    else:
        # No audio = no natural duration for the purple LED. Sleep briefly
        # so the wake-detected color reads as a single visible flash before
        # we transition to the listening color.
        time.sleep(0.2)

    # Fetch the next wake response in the background if provider is configured
    if _bg_executor is not None:
        _bg_executor.submit(fetch_next_wake_response)

    # Wake response audio is done — recording starts next. Flip from purple
    # (wake acknowledgment) to blue (actively listening for the command).
    set_led_transient("listening")


def _trim_wav_silence(wav_bytes: bytes, threshold: int = 200) -> bytes:
    """Strip leading/trailing silence from a WAV byte string.

    TTS providers commonly bookend output with 200-400ms of silence which
    bloats cached wake responses (where every ms costs perceived latency).
    Threshold is the abs sample value below which a frame counts as silent;
    default 200 ≈ -42 dB at 16-bit, conservative enough to not clip speech.
    """
    with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
        params = wav.getparams()
        frames = wav.readframes(wav.getnframes())

    if params.sampwidth != 2:  # 16-bit only — bail on float / 24-bit
        return wav_bytes

    samples = np.frombuffer(frames, dtype=np.int16)
    if params.nchannels > 1:
        samples = samples.reshape(-1, params.nchannels)
        active = np.any(np.abs(samples) > threshold, axis=1)
    else:
        active = np.abs(samples) > threshold

    if not active.any():
        return wav_bytes  # nothing above threshold, leave it alone

    first = int(active.argmax())
    last = len(active) - 1 - int(active[::-1].argmax())

    # Keep ~5ms pad on each side so we don't clip plosives. Aggressive
    # because every ms of leading silence is perceived latency on wake.
    pad = int(params.framerate * 0.005)
    first = max(0, first - pad)
    last = min(len(active) - 1, last + pad)

    trimmed = samples[first : last + 1]

    out = io.BytesIO()
    with wave.open(out, "wb") as wav_out:
        wav_out.setparams(params)
        wav_out.writeframes(trimmed.tobytes())
    return out.getvalue()


def fetch_next_wake_response():
    """Fetch the next wake response text and pre-generate audio cache."""
    try:
        provider = get_wake_response_provider()
        if not provider:
            logger.debug("No wake response provider configured")
            return

        response_text = provider.fetch_next_wake_response()
        if not response_text:
            return

        WAKE_FILE.write_text(response_text)
        logger.debug("Stored next wake response", response=response_text)

        # Pre-generate audio so next wake word plays instantly
        command_center_url = get_command_center_url()
        if not command_center_url:
            return

        audio_bytes: bytes | None = RestClient.post_binary(
            f"{command_center_url}/api/v0/media/tts/speak",
            data={"text": response_text},
            timeout=30,
        )
        if audio_bytes:
            original_size = len(audio_bytes)
            try:
                audio_bytes = _trim_wav_silence(audio_bytes)
            except Exception as e:
                logger.debug("Silence trim failed, using original", error=str(e))
            WAKE_AUDIO_FILE.write_bytes(audio_bytes)
            logger.debug(
                "Cached wake response audio",
                size_bytes=len(audio_bytes),
                trimmed_from=original_size,
            )

    except Exception as e:
        logger.error("Failed to fetch next greeting", error=str(e))


def play_processing_ack() -> bool:
    """Play the pre-cached processing ack in a background thread.

    Non-blocking so STT + LLM can start immediately — the ack is meant
    to MASK their latency, not precede it. Returning True tells the
    caller an ack will play, so it can suppress the delayed ack timer
    to avoid double-acking.
    """
    if not PROCESSING_ACK_FILE.exists():
        return False

    def _play_and_cleanup() -> None:
        try:
            platform_audio.play_audio_file(str(PROCESSING_ACK_FILE))
        except Exception as e:
            logger.warning("Failed to play processing ack", error=str(e))
        finally:
            PROCESSING_ACK_FILE.unlink(missing_ok=True)

    if _bg_executor is not None:
        _bg_executor.submit(_play_and_cleanup)
    return True


def fetch_next_processing_ack() -> None:
    """Pre-generate a processing ack WAV for the next interaction.

    Mirrors :func:`fetch_next_wake_response` — picks a random short ack,
    synthesises audio via TTS, and caches it to disk so the next wake
    cycle can play it instantly after recording ends.
    """
    try:
        command_center_url = get_command_center_url()
        if not command_center_url:
            return

        text = random.choice(_PROCESSING_ACK_POOL)
        audio_bytes: bytes | None = RestClient.post_binary(
            f"{command_center_url}/api/v0/media/tts/speak",
            data={"text": text},
            timeout=15,
        )
        if audio_bytes:
            PROCESSING_ACK_FILE.write_bytes(audio_bytes)
            logger.debug("Cached processing ack audio", text=text, size_bytes=len(audio_bytes))
    except Exception as e:
        logger.debug("Failed to pre-generate processing ack (non-fatal)", error=str(e))
