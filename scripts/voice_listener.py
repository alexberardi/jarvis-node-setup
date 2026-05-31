import os
import queue
import random
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Dict, Iterator

import numpy as np
import openwakeword
from openwakeword.model import Model as OWWModel
import pyaudio
# scipy.signal is lazy-imported below — pulling it in at module top
# eagerly loads scipy + sklearn + ~50 MB of compiled extensions even
# when MCL_ONFAULT is in play (the import itself touches every page).
# Deferring to first call keeps those pages cold until/unless wake
# detection actually runs (which it always does, but only after all
# other startup state has settled, smoothing the boot RSS curve).
_resample_poly = None
from jarvis_log_client import JarvisLogger


def _get_resample_poly():
    """Lazy-import scipy.signal.resample_poly on first audio chunk."""
    global _resample_poly
    if _resample_poly is None:
        from scipy.signal import resample_poly  # noqa: E402
        _resample_poly = resample_poly
    return _resample_poly

from clients.rest_client import RestClient
from core.aec_pipeline import AecPipeline
from core.audio_bus import AudioBus
from core.barge_in import BargeInMonitor
from core.helpers import get_tts_provider, get_stt_provider, get_wake_response_provider
from core.platform_audio import platform_audio
from scripts.speech_to_text import (
    RecordingResult,
    concat_wav_files,
    listen,
    listen_for_follow_up,
    snapshot_bus_to_wav,
)
from services.alert_queue_service import get_alert_queue_service
from utils.config_service import Config
from utils.command_execution_service import CommandExecutionService
from utils.encryption_utils import get_cache_dir
from utils.service_discovery import get_command_center_url
from clients.responses.jarvis_command_center import ValidationRequest

logger = JarvisLogger(service="jarvis-node")

# Bounded pool for fire-and-forget background tasks (wake response fetch,
# processing ack generation, audio playback).  Prevents thread leaks — bare
# threading.Thread() calls were leaving 1-2 orphan threads per voice command.
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-bg")

CHIME_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sounds", "chime.wav")
_cache_dir = get_cache_dir()
WAKE_FILE = _cache_dir / "next_wake_response.txt"
WAKE_AUDIO_FILE = _cache_dir / "next_wake_response.wav"
PROCESSING_ACK_FILE = _cache_dir / "next_processing_ack.wav"

# Short, snappy acks played immediately after recording ends to fill the
# dead air while STT + LLM process.  No LLM needed — just variety.
_PROCESSING_ACK_POOL: list[str] = [
    "One moment.",
    "Got it.",
    "Working on it.",
    "Let me check.",
    "On it.",
    "Give me a second.",
]

# wake_word_model is baked into the openWakeWord instance loaded at startup,
# so changing it still requires a service restart. Everything else below is
# read fresh on each wake / follow-up cycle so mobile-app updates apply live.
WAKE_WORD_MODEL = Config.get_str("wake_word_model", "hey_jarvis") or "hey_jarvis"

# Wake-threshold auto-calibration state ---------------------------------------
# Tracks OWW scores at wake-fire for wakes that resulted in a legitimate
# (non not_for_me) interaction. When wake_word_threshold_auto is enabled,
# _wake_threshold() uses these to set a per-node threshold instead of a
# static default. Persisted to disk so the threshold survives restarts.
_WAKE_SCORE_HISTORY_FILE = _cache_dir / "wake_scores.json"
_WAKE_SCORE_HISTORY_MAX = 20
_WAKE_SCORE_HISTORY_MIN_SAMPLES = 5
_wake_score_history: deque[float] = deque(maxlen=_WAKE_SCORE_HISTORY_MAX)
_wake_score_history_lock = threading.Lock()
_wake_score_history_loaded = False


def _load_wake_score_history() -> None:
    """Load persisted wake-score history into the in-memory deque (idempotent)."""
    global _wake_score_history_loaded
    if _wake_score_history_loaded:
        return
    try:
        if _WAKE_SCORE_HISTORY_FILE.exists():
            import json as _json
            data = _json.loads(_WAKE_SCORE_HISTORY_FILE.read_text())
            if isinstance(data, list):
                with _wake_score_history_lock:
                    _wake_score_history.clear()
                    for s in data[-_WAKE_SCORE_HISTORY_MAX:]:
                        if isinstance(s, (int, float)) and 0.0 <= float(s) <= 1.0:
                            _wake_score_history.append(float(s))
    except (OSError, ValueError) as e:
        logger.warning("Failed to load wake score history", error=str(e))
    finally:
        _wake_score_history_loaded = True


def _record_legitimate_wake_score(score: float) -> None:
    """Track a wake-fire score that produced a non-not_for_me interaction.

    Each legitimate wake is one data point telling the calibrator "this is
    what 'hey jarvis' from the user sounds like in this room." After enough
    samples, _auto_calibrated_wake_threshold puts the threshold just under
    the lowest-scoring real wake — meaning real wakes always fire on the
    first attempt without enlarging the false-positive window for ambient
    noise. Persisted to disk so the calibration survives restarts.
    """
    if not (0.0 <= score <= 1.0):
        return
    with _wake_score_history_lock:
        _wake_score_history.append(float(score))
        snapshot = list(_wake_score_history)
    try:
        import json as _json
        _WAKE_SCORE_HISTORY_FILE.write_text(_json.dumps(snapshot))
    except OSError as e:
        logger.warning("Failed to persist wake score history", error=str(e))


def _auto_calibrated_wake_threshold(fallback: float) -> float:
    """Return a dynamic wake threshold based on recent legitimate-wake scores.

    Uses the 20th-percentile of recent scores discounted by 15%, clamped
    to [0.10, 0.50]. The p20 anchor gives us a number 80% of real wakes
    exceed; the 15% discount provides margin for normal variability.
    Falls back to ``fallback`` until we have ``_WAKE_SCORE_HISTORY_MIN_SAMPLES``
    data points (no calibration on a fresh node).
    """
    _load_wake_score_history()
    with _wake_score_history_lock:
        scores = sorted(_wake_score_history)
    if len(scores) < _WAKE_SCORE_HISTORY_MIN_SAMPLES:
        return fallback
    idx = int(len(scores) * 0.20)
    p20 = scores[idx]
    calibrated = p20 * 0.85
    return max(0.10, min(0.50, calibrated))


def _wake_threshold() -> float:
    """Wake-word detection threshold, lowered while music is playing.

    Music coming out of the same speaker the mic is hearing reduces the
    model's confidence in "hey jarvis" — without AEC, the user's voice is
    competing with their own playback in the mic stream. Drop the threshold
    aggressively in that case (mirrors barge_in's 0.07) and pair it with
    the energy gate in the wake loop so music alone can't trigger a wake
    even though it scores 0.12-0.18 fairly often.

    Was 0.25 before the May-2026 beta: 0.25 was almost never crossed
    against typical music + voice combinations on the Pi Zero mic, so
    "stop the music" verbally was unreliable. The 0.12 floor + energy
    gate together produce both higher recall AND lower false-positive
    rate than either alone.

    When ``wake_word_threshold_auto`` is enabled, the non-music threshold
    is derived from observed wake scores via ``_auto_calibrated_wake_threshold``
    — per-node, per-mic, per-room. Default off so existing nodes' static
    threshold isn't silently replaced.
    """
    if _music_is_playing():
        return Config.get_float("wake_word_threshold_music", 0.12)
    static_default = Config.get_float("wake_word_threshold", 0.4)
    if Config.get_bool("wake_word_threshold_auto", False):
        return _auto_calibrated_wake_threshold(static_default)
    return static_default


def _wake_music_energy_multiplier() -> float:
    """How far current RMS must rise above the running baseline to fire
    a wake during music playback.

    Music alone occupies a fairly stable RMS band — a voice spoken over
    it adds energy on top, producing a spike of ~1.5-2.5x the music
    baseline at a normal speaking distance from the Pi Zero mic.
    Tunable via the ``wake_word_music_energy_multiplier`` setting if
    the room's speaker bleed profile is unusual.
    """
    return Config.get_float("wake_word_music_energy_multiplier", 1.5)


def _music_is_playing() -> bool:
    """True if any tracked media-player has an UNCORKED PulseAudio sink-input.

    Process existence alone is misleading: spotifyd runs as a daemon 24/7
    listening for Spotify Connect commands, regardless of whether music is
    actually playing. The reliable signal is PA's cork state — a sink-input
    is uncorked iff the application is actively producing audio (including
    when we've SIGSTOP'd the process; the cork stays in the same state
    until SIGCONT). Falls back to False on any pactl failure rather than
    raising the wake threshold unnecessarily.
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
    import json as _json
    try:
        items = _json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        return False
    for item in items:
        props = item.get("properties") or {}
        binary = props.get("application.process.binary") or ""
        if binary in _PLAYER_BINARIES and not item.get("corked", True):
            return True
    return False


# --- Music ducking ----------------------------------------------------------
# When a wake word fires, pause any active media-player subprocesses for the
# duration of the conversation so they don't compete with the user's voice
# (and so we don't have to fight AEC). SIGSTOP halts the process at the
# kernel level — the player's audio output stops immediately; SIGCONT resumes
# from where it was paused. This is surgical: jarvis's own wake response and
# TTS audio play at full volume because they're emitted from the jarvis-node
# process, which is never the target of these signals.
#
# We pause by binary name (mpv / ffplay / cvlc / vlc) rather than by tracking
# specific PIDs because commands can spawn and exit players asynchronously —
# Pandora's _play_next() auto-advances, so the PID we'd track could be stale
# by the time we resume. Name-based covers all in-flight players atomically.
#
# If a command explicitly stops playback (e.g. "stop the music"), the player
# process terminates on its own — pkill -CONT against a missing process is
# a harmless no-op.
# Binaries that can be safely SIGSTOP'd while ducking: unidirectional consumers
# (they read from an upstream source and write to PA; pausing the process just
# halts both ends without breaking any local protocol). mpv/ffplay/cvlc/vlc
# typically stream HTTP and don't care if reads stall; spotifyd talks to
# Spotify's cloud, again no local protocol waiting on it.
_SIGSTOP_PLAYER_BINARIES: tuple[str, ...] = (
    "mpv", "ffplay", "cvlc", "vlc",
    "spotifyd",      # jarvis-cmd-spotify (pre-v0.1.3, kept for backwards
                     # compat — pkill of a missing binary is a no-op)
    "librespot",     # jarvis-cmd-spotify v0.1.3–v1.x (apt-installed via
                     # the raspotify package)
    "go-librespot",  # jarvis-cmd-spotify v2.x+ — bundled binary controlled
                     # via its localhost HTTP API; same Connect protocol,
                     # different process name. Without this entry the wake-
                     # word ducking misses Spotify entirely and music
                     # bleeds into the user's voice capture.
)

# Binaries that must NOT be SIGSTOP'd because they participate in a
# request/response protocol with a remote peer that expects timely ACKs.
# shairport-sync is the canonical example: SIGSTOP'ing it makes Music
# Assistant's RTSP TEARDOWN hang (MA can't update queue state → UI stuck
# showing "playing" after voice-stop). Muting the PA sink-input alone is
# sufficient — shairport keeps running and answering RTSP, but its audio
# output reaches a muted sink during the conversation.
_MUTE_ONLY_PLAYER_BINARIES: tuple[str, ...] = (
    "shairport-sync",  # jarvis-cmd-music-assistant (AirPlay receiver for MA streams)
)

# Union — used by the PA sink-input matcher, which mutes everything regardless
# of pause mechanism.
_PLAYER_BINARIES: tuple[str, ...] = _SIGSTOP_PLAYER_BINARIES + _MUTE_ONLY_PLAYER_BINARIES


def _player_sink_input_ids() -> list[str]:
    """Return PA sink-input ids belonging to known media-player processes.

    SIGSTOP'ing the player only stops *new* audio production; up to several
    seconds of audio may already be buffered in PA's sink-input. To silence
    the speaker immediately we mute those sink-inputs directly. pactl reports
    `application.process.binary` for each sink-input — we match by that.
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
    import json as _json
    try:
        items = _json.loads(result.stdout or "[]")
    except (ValueError, TypeError):
        return []
    ids: list[str] = []
    for item in items:
        props = item.get("properties") or {}
        binary = props.get("application.process.binary") or ""
        if binary in _PLAYER_BINARIES:
            sid = item.get("index")
            if sid is not None:
                ids.append(str(sid))
    return ids


def _pause_active_playback() -> None:
    """Silence any active media-player subprocesses immediately.

    Two-pronged: SIGSTOP halts the process so it stops producing new audio,
    AND we mute its PA sink-input so audio already buffered by PA doesn't
    keep playing while the listener captures the user's command. Without
    the sink-input mute, spotifyd's PA buffer (~1-5s of audio) bleeds into
    the mic and Whisper transcribes the music instead of the user's speech
    (returns markers like "(wind blowing)" / "(music)").

    No internal "is-paused" flag: a previous version tracked state in a
    global so overlapping wake events wouldn't double-pause, but the flag
    drifted out of sync when wake events landed without any player running,
    then stuck at True — preventing the actual pause on the NEXT wake when
    music WAS playing. pkill/pactl against missing targets is harmless.
    """
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
    muted: list[str] = []
    for sink_input_id in _player_sink_input_ids():
        try:
            r = subprocess.run(
                ["pactl", "set-sink-input-mute", sink_input_id, "1"],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                muted.append(sink_input_id)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    logger.info(
        "pause_active_playback",
        sigstopped=stopped, muted_sink_inputs=muted,
    )


def _resume_active_playback() -> None:
    """Reverse the duck: unmute the player sink-inputs, then SIGCONT."""
    # Unmute BEFORE SIGCONT — otherwise SIGCONT releases the process which
    # immediately writes audio to a still-muted sink-input (silently
    # discarded), then we unmute and the user hears resumed playback half
    # a beat later than the response finished. Unmuting first means the
    # buffered audio re-engages the moment we SIGCONT.
    unmuted: list[str] = []
    for sink_input_id in _player_sink_input_ids():
        try:
            r = subprocess.run(
                ["pactl", "set-sink-input-mute", sink_input_id, "0"],
                timeout=2.0, capture_output=True,
            )
            if r.returncode == 0:
                unmuted.append(sink_input_id)
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
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
    logger.info(
        "resume_active_playback",
        unmuted_sink_inputs=unmuted, sigcont=resumed,
    )


# Kept for backwards-compat with older callers; aliases to the new pause flow.
_duck_music = _pause_active_playback
_restore_music = _resume_active_playback


def _barge_in_enabled() -> bool:
    raw = Config.get_str("barge_in_enabled", "true") or "true"
    return raw.lower() in ("true", "1", "yes")


def _barge_in_threshold() -> float:
    # Matches BargeInMonitor's _DEFAULT_OWW_THRESHOLD — was 0.07, lowered
    # to 0.04 after beta logs showed real "Hey Jarvis"-over-TTS scores
    # peaking at 0.05-0.10 (right at the old floor). confirm_chunks=2
    # plus the recent_max_rms energy gate keep false positives bounded.
    return Config.get_float("barge_in_threshold", 0.04)


def _barge_in_energy_threshold() -> float:
    return Config.get_float("barge_in_energy_threshold", 500.0)

# Follow-up loop safety limits — prevents ambient noise from keeping
# the conversation alive indefinitely (the "perpetual follow-up" bug).
MAX_FOLLOW_UP_ITERATIONS = 5    # Hard cap on follow-up iterations
MAX_CONSECUTIVE_NOISE = 2       # Exit after N consecutive noise transcriptions
FOLLOW_UP_TIMEOUT_DECAY = 2.0   # Shorten listen window by this per iteration (s)
FOLLOW_UP_MIN_TIMEOUT = 3.0     # Floor for the decayed timeout (s)

# openWakeWord needs 16 kHz audio in 1280-sample (80 ms) chunks
OWW_RATE = 16000
OWW_CHUNK = 1280

# Many USB mics only support 44100/48000 Hz — capture at 48 kHz and downsample
MIC_RATE = 48000
MIC_CHUNK = OWW_CHUNK * (MIC_RATE // OWW_RATE)  # 3840 samples at 48 kHz = 80 ms

# Each captured chunk is exactly one OWW frame = 80 ms of audio.
_CHUNK_SECONDS: float = OWW_CHUNK / OWW_RATE  # 0.08
# Pre-wake VAD: keep ~5 s of "was-speech" frames in a ring buffer so we can
# tell the command-center "the room had ongoing speech for the N seconds
# before this wake fired." Mid-conversation wakes (the false-wake case we
# most want to silence) jump out because the buffer is mostly True; a user
# walking up and saying "hey Jarvis what's the weather" has a near-empty
# buffer. The CC LLM uses this as a direction hint for its not_for_me call.
PRE_WAKE_VAD_WINDOW_SECS: float = 5.0
PRE_WAKE_VAD_FRAMES: int = max(1, int(PRE_WAKE_VAD_WINDOW_SECS / _CHUNK_SECONDS))


def _pre_wake_vad_threshold() -> float:
    """RMS (int16) above which we count a frame as speech-like.

    The dev-Pi USB mic showed baseline-ambient RMS sustained in the
    high-hundreds-to-low-thousands range, so the original 500 default
    flagged ordinary "quiet room" frames as speech. 2500 separates
    that ambient floor from a person actually speaking. Per-room mic
    tuning lives in the ``pre_wake_vad_rms_threshold`` setting so we
    can adjust without a redeploy once we have observed values.
    """
    return Config.get_float("pre_wake_vad_rms_threshold", 2500.0)


def _adaptive_silence_threshold(rms_stats: dict[str, float]) -> int | None:
    """Derive a per-cycle silence_threshold from the pre-wake noise floor.

    The 5 s pre-wake RMS window is overwhelmingly ambient (the wake word
    itself is only the last ~0.25 s), so its median is a clean read of
    room noise at the instant of wake. We lift the threshold to a
    multiple of that floor so normal speech (typically RMS 1000-3000+)
    easily clears it but breath/HVAC/fridge-hum bursts don't — without
    forcing the operator to hand-tune a static value that's right for
    one time of day and wrong for another (the kitchen-Pi failure mode
    that motivated this).

    Returns ``None`` to mean "fall back to the static config value":
    either auto-mode is disabled, the stats deque was empty, or the
    multiplier produced something obviously wrong.

    Bounds: ``[200, 1500]``. Below 200 the recorder treats sub-baseline
    HVAC ticks as silence-breaks and never stops; above 1500 it starts
    cutting into normal speech amplitude.
    """
    if not Config.get_bool("silence_threshold_auto", True):
        return None
    if not isinstance(rms_stats, dict):
        return None
    median = rms_stats.get("median")
    if not isinstance(median, (int, float)) or median <= 0:
        return None
    # Multiplier 2.0 (was 3.0) and ceiling 1000 (was 1500): the original
    # 3× was tuned on quiet-room data (median 130-170 → threshold 400-500),
    # but in a loud room (median 470, kitchen with fan/TV) 3× produces
    # 1410 — above typical command-speech amplitude (1000-2500). That
    # clipped multi-syllable commands mid-sentence; Whisper got fragments
    # and hallucinated short words like "Bye." that then hit the filter.
    # Bias is toward NOT clipping the user: a too-low threshold lengthens
    # the recording (recoverable), a too-high one drops the command
    # (not recoverable).
    multiplier = Config.get_float("silence_threshold_auto_multiplier", 2.0)
    floor = Config.get_int("silence_threshold_auto_floor", 200)
    ceiling = Config.get_int("silence_threshold_auto_ceiling", 1000)
    return max(floor, min(ceiling, int(median * multiplier)))


_WAKE_CHIMES_DIR = Path(__file__).resolve().parent.parent / "sounds" / "wake"

# Track the last identified speaker so parallel warmup can load their memories
# and CC can use it for per-node stickiness on short follow-up utterances.
# Updated after every successful transcription with speaker identification.
_last_speaker_user_id: int | None = None
_last_speaker_confidence: float | None = None

# Shared AudioBus, set when start_voice_listener() initializes its bus.
# Other subsystems (e.g. enrollment-via-MQTT) need a way to consume mic
# audio without opening a competing PyAudio stream — they call
# ``get_audio_bus()`` and subscribe.
_audio_bus: AudioBus | None = None

# ``oww_lock`` lives in ``core.barge_in`` because BargeInMonitor also
# calls oww.predict() and oww.reset() on the same model. Sharing one
# lock across both files keeps every predict/reset serialized — single
# concurrent operation against the model at any moment.
from core.barge_in import oww_lock as _oww_lock


def _locked_oww_reset(oww_model) -> None:
    """Reset oww under the shared lock. Submit to ``_bg_executor`` to
    keep the wake-hot-path unblocked while still serializing with any
    in-flight ``oww.predict()``.
    """
    _t = time.monotonic()
    with _oww_lock:
        oww_model.reset()
    logger.info(
        f"⏱️ wake-step | background oww.reset finished in "
        f"{int((time.monotonic() - _t) * 1000)}ms"
    )


def get_audio_bus() -> AudioBus | None:
    """Return the running AudioBus, or None if voice_listener hasn't started."""
    return _audio_bus


# When set, the main wake loop short-circuits its score check and yields
# the CPU. Used by transient flows that want to consume the mic via the
# bus without competing with wake detection — voice-profile enrollment
# in particular, where reading a sample prompt aloud near the mic would
# otherwise re-fire the wake detector and clash with the recording.
_wake_paused = threading.Event()

# Dedupe back-to-back wake fires for a single utterance.
# openWakeWord can score >threshold on consecutive 80ms chunks for one
# "Hey Jarvis", and the wake loop break can re-trigger before the
# conversation flow takes the lock. This guard ignores any wake whose
# previous trigger was less than _WAKE_DEBOUNCE_SEC ago.
_WAKE_DEBOUNCE_SEC = 8.0
_last_wake_ts: float = 0.0
_last_wake_lock = threading.Lock()


def pause_wake() -> None:
    """Disable wake detection until ``resume_wake()`` is called."""
    _wake_paused.set()
    logger.debug("Wake detection paused")


def resume_wake() -> None:
    """Re-enable wake detection."""
    _wake_paused.clear()
    logger.debug("Wake detection resumed")


@contextmanager
def wake_paused() -> Iterator[None]:
    """``with`` block that disables wake detection for its duration.

    Usage::

        from scripts.voice_listener import wake_paused
        with wake_paused():
            # capture mic via the bus without wake firing on what we hear
            ...
    """
    pause_wake()
    try:
        yield
    finally:
        resume_wake()


def _run_warmup(
    command_service: CommandExecutionService,
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


def _set_led_transient(pattern: str | None) -> None:
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
            with wake_paused():
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
                with wake_paused():
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
    _set_led_transient("wake_detected")

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
    _bg_executor.submit(fetch_next_wake_response)

    # Wake response audio is done — recording starts next. Flip from purple
    # (wake acknowledgment) to blue (actively listening for the command).
    _set_led_transient("listening")


def _trim_wav_silence(wav_bytes: bytes, threshold: int = 200) -> bytes:
    """Strip leading/trailing silence from a WAV byte string.

    TTS providers commonly bookend output with 200-400ms of silence which
    bloats cached wake responses (where every ms costs perceived latency).
    Threshold is the abs sample value below which a frame counts as silent;
    default 200 ≈ -42 dB at 16-bit, conservative enough to not clip speech.
    """
    import io
    import wave

    import numpy as np

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


def _play_processing_ack() -> bool:
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

    _bg_executor.submit(_play_and_cleanup)
    return True


def _fetch_next_processing_ack() -> None:
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


def _make_validation_handler(bus: AudioBus, stt_provider) -> Callable[[ValidationRequest], str]:
    """Create a validation handler that prompts via TTS and re-listens."""
    def validation_handler(validation: ValidationRequest) -> str:
        tts_provider_instance = get_tts_provider()

        question = validation.question
        if validation.options:
            options_text = ", ".join(validation.options)
            question = f"{question} Your options are: {options_text}"

        logger.info("Asking validation question", question=question)
        tts_provider_instance.speak(False, question)

        logger.debug("Listening for validation response")
        validation_recording = listen(bus, history_secs=0.0)

        validation_transcription = stt_provider.transcribe(validation_recording.audio_file)

        if validation_transcription:
            logger.info("User validation response", response=validation_transcription)
            return validation_transcription
        else:
            logger.warning("Failed to transcribe validation response")
            return "I didn't catch that, sorry."

    return validation_handler


def _is_non_speech(text: str | None) -> bool:
    """True if Whisper output is a non-transcript — empty, whitespace, or
    a bracketed annotation like [BLANK_AUDIO] / (wind blowing) that Whisper
    emits for silence and noise rather than user speech.

    This NO LONGER filters "hallucination phrases" (single-word fillers,
    YouTube artifacts, etc.). That blocklist was eating real commands
    like "okay" / "bye" / "yes". The right place to decide "is this a
    real command" is CC's LLM ``<not_for_me/>`` classifier, which has
    full context — speaker, prior turns, available tools — that a static
    word list never will. We only drop things that aren't transcripts at
    all here; anything that looks like words goes to CC.
    """
    if not text:
        return True
    stripped = text.strip()
    if not stripped:
        return True
    # Whisper-emitted metadata: [BLANK_AUDIO], (silence), [music], etc.
    # These are annotations, not utterances; sending them to the LLM
    # wastes a round-trip for no possible useful outcome.
    if (
        (stripped.startswith("[") and stripped.endswith("]"))
        or (stripped.startswith("(") and stripped.endswith(")"))
    ):
        return True
    # "..." is a Whisper non-speech marker, not an utterance.
    if stripped.strip(".") == "":
        return True
    return False


# Words that are valid as standalone single-word follow-ups (control
# verbs the LLM doesn't need to disambiguate). Used by the follow-up
# loop's local noise gate, not by _is_non_speech.
_VALID_FOLLOW_UP_WORDS: set[str] = {
    "stop", "pause", "resume", "help", "repeat",
    "louder", "quieter", "cancel", "continue",
}


_ECHO_STOPWORDS: set[str] = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "and", "or", "for", "in", "on", "at", "by", "with",
    "i", "you", "it", "we", "they", "he", "she", "me", "us", "them",
    "this", "that", "these", "those", "here", "there",
    "do", "does", "did", "have", "has", "had", "will", "would", "can",
    "could", "should", "may", "might", "must",
    "what", "when", "where", "why", "how", "who",
}


def _looks_like_self_echo(text: str, last_assistant_text: str) -> bool:
    """True if the follow-up transcript looks like the node hearing its
    own TTS response instead of the user.

    Secondary defense behind the post-TTS settle delay in ``_follow_up_loop``
    — if PA buffer drain ran long, the mic still picks up the TTS tail and
    Whisper transcribes it. Without this check, that transcript flows
    straight back into CC and produces phantom follow-ups (the "Here is
    sad music" beta blocker, May 2026).

    Heuristic: compare significant (non-stopword) word sets.

    * ≥3 significant user-side words and ≥85% overlap → echo.
    * 2 significant user-side words and 100% overlap → echo (this is
      what catches the "Here is sad music" canonical case: stopwords
      strip to {sad, music}, both already in the assistant's reply).

    The threshold is intentionally high so a legitimate user reply that
    quotes part of the assistant ("yes, set it for 8 PM" against
    "I'll set the alarm for 8 PM") doesn't get suppressed — that case
    overlaps on {set, timer/alarm, pm} which is well below 85%.
    """
    if not text or not last_assistant_text:
        return False
    import re as _re
    user_words = {w for w in _re.findall(r"[a-z']+", text.lower()) if len(w) >= 2}
    user_words -= _ECHO_STOPWORDS
    if len(user_words) < 2:
        return False
    assistant_words = {w for w in _re.findall(r"[a-z']+", last_assistant_text.lower()) if len(w) >= 2}
    overlap = len(user_words & assistant_words)
    if len(user_words) == 2:
        return overlap == 2
    return overlap / len(user_words) >= 0.85


def _extract_assistant_text(result: dict | None) -> str:
    """Best-effort extraction of the spoken text from a CC result dict.

    CC's voice/command response shapes vary slightly between code paths;
    we look at the most likely keys and fall back to empty. Empty means
    the echo check sits dormant (no false positives) — graceful.
    """
    if not isinstance(result, dict):
        return ""
    for key in ("message", "text", "response", "spoken_text", "assistant_text"):
        val = result.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return ""


def _is_follow_up_noise(text: str, prev_text: str | None) -> bool:
    """Detect ambient noise Whisper transcribed as speech during follow-up.

    More conservative than ``_is_non_speech`` — this only runs in the
    follow-up loop where we're skeptical about whether audio was directed
    at the device.  Catches two patterns:

    1. **Exact repeat** — Whisper hallucinating the same phrase from
       similar ambient noise on consecutive iterations.
    2. **Lone word** — Single generic words that aren't valid commands.
       Real follow-ups are almost never one isolated word.
    """
    if not text:
        return True

    stripped = text.strip()
    lowered = stripped.lower().rstrip(".!,?")
    words = lowered.split()

    # Exact repeat of previous transcription
    if prev_text:
        prev_lowered = prev_text.strip().lower().rstrip(".!,?")
        if lowered == prev_lowered:
            return True

    # Single word not in the valid-command set
    if len(words) == 1 and lowered not in _VALID_FOLLOW_UP_WORDS:
        return True

    return False


_ABORT_PHRASES: set[str] = {
    "never mind", "nevermind", "cancel", "forget it",
    "that wasn't for you", "not you", "sorry jarvis",
    "ignore that", "ignore me",
}


def _is_false_wake(transcription: str, recording: RecordingResult) -> bool:
    """Detect false wake word triggers from ambient conversation.

    Uses a combination of signals:
    1. Abort phrases — user heard the chime and wants to cancel
    2. Max recording duration + long/mid-sentence transcription — ambient speech
    """
    raw = transcription.strip()
    text = raw.lower()

    # Signal 1: abort phrases
    for phrase in _ABORT_PHRASES:
        if text == phrase or text.startswith(phrase):
            return True

    # Signal 2: recording hit max duration (speaker never paused)
    if recording.hit_max_duration:
        words = text.split()
        # Long transcription — ambient conversation, not a command
        if len(words) > 20:
            return True
        # Starts mid-sentence (lowercase in original, not "i" or "ok")
        if raw and raw[0].islower() and not text.startswith(("i ", "i'", "ok")):
            return True

    return False


_ERRORS_DIR = Path(__file__).resolve().parent.parent / "sounds" / "errors"


# --- Wake-word concat (Phase 2d) ---
#
# At wake-fire time we snapshot the bus ring buffer to capture the
# wake-word audio that's about to be discarded (listen() runs with
# history_secs=0 so Whisper's transcription stays clean). That snapshot
# is then concatenated with the just-recorded command audio and sent as
# the `speaker_audio` field to whisper-api — ECAPA scores the longer
# combined clip while Whisper still transcribes the command-only file.
# The same wake snapshot is reused for every follow-up in the same
# conversation since follow-ups have no wake word of their own.
_WAKE_AUDIO_PATH = _cache_dir / "wake.wav"
_SPEAKER_AUDIO_PATH = _cache_dir / "speaker.wav"
_WAKE_SNAPSHOT_SECONDS = 2.0


def _try_capture_wake_audio(bus: AudioBus) -> str | None:
    """Snapshot the wake-word audio from the bus ring buffer.

    Returns the wake WAV path on success, None if nothing was captured
    or the write failed (callers proceed without speaker_audio in that
    case — recognition still works, just on the command-only clip).
    """
    try:
        captured = snapshot_bus_to_wav(
            bus, _WAKE_SNAPSHOT_SECONDS, str(_WAKE_AUDIO_PATH),
        )
    except Exception as e:
        logger.warning("Wake-audio snapshot failed (continuing without)", error=str(e))
        return None
    return str(_WAKE_AUDIO_PATH) if captured else None


def _try_build_speaker_audio(
    wake_audio_path: str | None, command_audio_path: str,
) -> str | None:
    """Concat wake-word audio with the command audio for the speaker pass.

    Returns the concat WAV path, or None if there's no wake audio to
    prepend or the concat failed (STT falls back to single-file pass).
    """
    if not wake_audio_path:
        return None
    try:
        concat_wav_files(
            wake_audio_path, command_audio_path, str(_SPEAKER_AUDIO_PATH),
        )
    except Exception as e:
        logger.warning(
            "Speaker-audio concat failed (using command-only)",
            error=str(e),
        )
        return None
    return str(_SPEAKER_AUDIO_PATH)


def _speak_error(message: str) -> None:
    """Speak an error message, falling back to a bundled sound if TTS fails."""
    _set_led_transient("error")
    try:
        tts = get_tts_provider()
        tts.speak(False, message)
    except Exception:
        chime = _ERRORS_DIR / "error_generic.wav"
        if chime.exists():
            platform_audio.play_audio_file(str(chime))
    finally:
        _set_led_transient(None)


def send_for_transcription(
    recording: RecordingResult,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
    warmup_thread: threading.Thread | None = None,
    conversation_id: str | None = None,
    warmup_result: dict | None = None,
    skip_ack: bool = False,
    pre_wake_speech_seconds: float | None = None,
    wake_audio_path: str | None = None,
) -> Dict[str, Any] | None:
    global _last_speaker_user_id, _last_speaker_confidence

    logger.info("Sending audio to transcription server")
    _set_led_transient("thinking")

    # Build a longer speaker-pass clip (wake-word + command) when we
    # have the wake audio captured. Whisper still transcribes the
    # command-only file at recording.audio_file. See _try_build_speaker_audio.
    speaker_audio_path = _try_build_speaker_audio(
        wake_audio_path, recording.audio_file,
    )

    # STT with specific error handling
    try:
        result = stt_provider.transcribe_with_speaker(
            recording.audio_file, speaker_audio_path=speaker_audio_path,
        )
    except (ConnectionError, OSError, TimeoutError) as e:
        logger.error("STT connection failed", error=str(e))
        _speak_error("I'm having trouble connecting right now.")
        _set_led_transient(None)
        return None
    except Exception as e:
        logger.error("STT failed", error=str(e))
        _speak_error("I couldn't understand that, sorry.")
        _set_led_transient(None)
        return None

    if _is_non_speech(result.text):
        logger.info("Non-speech transcription, skipping", text=result.text)
        _set_led_transient(None)
        return None

    if result.text and _is_false_wake(result.text, recording):
        logger.info("False wake detected, aborting silently", text=result.text[:80],
                     duration=recording.duration, hit_max=recording.hit_max_duration)
        _set_led_transient(None)
        return None

    if result.text:
        transcription = result.text
        speaker_user_id = result.speaker_user_id
        if speaker_user_id:
            _last_speaker_user_id = speaker_user_id
            _last_speaker_confidence = result.speaker_confidence
            logger.info("Transcription received", text=transcription,
                        speaker_user_id=speaker_user_id, speaker_confidence=result.speaker_confidence)
        else:
            logger.info("Transcription received", text=transcription)

        # Command processing with specific error handling.
        # When wake_ack_audio_enabled=False the user has opted out of audio
        # acks entirely — no wake-time ack, no post-listen processing-ack,
        # no LLM-side "let me look into that" ack. The LED still flashes.
        audio_acks_disabled = not Config.get_bool("wake_ack_audio_enabled", True)
        try:
            result = command_service.process_voice_command(
                transcription, validation_handler,
                speaker_user_id=speaker_user_id,
                conversation_id=conversation_id,
                warmup_thread=warmup_thread,
                warmup_result=warmup_result,
                skip_ack=skip_ack or audio_acks_disabled,
                pre_wake_speech_seconds=pre_wake_speech_seconds,
            )
        except (ConnectionError, OSError, TimeoutError) as e:
            logger.error("Command center unreachable", error=str(e))
            _speak_error("I can't reach my server right now.")
            return None
        except Exception as e:
            logger.error("Command processing failed", error=str(e))
            _speak_error("Something went wrong, sorry about that.")
            return None

        command_service.speak_result(result)
        return result
    else:
        _speak_error("I couldn't understand that, sorry.")
        return None


def _follow_up_loop(
    bus: AudioBus,
    initial_result: dict | None,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
    oww=None,
    tts_end_ts: float | None = None,
    wake_audio_path: str | None = None,
    silence_threshold: int | None = None,
    silence_duration: float | None = None,
) -> None:
    """Listen for follow-up speech after TTS completes.

    If the user speaks within the follow-up window, process it as a
    continuation of the conversation. Each successful follow-up restarts
    the timer. Silence or error breaks out to wake word mode.

    If ``oww`` is provided, barge-in monitoring is active during TTS
    playback — the wake word interrupts the response and returns to the
    main wake detection loop.
    """
    # Default 4s (was 10s): in beta deployments 10s felt overkill —
    # users either follow up within 2-3s or have moved on entirely, and
    # the long open window leaves the LED in "listening" too long after
    # the response. 4s still covers the typical 2-3s reaction time.
    # Users can override via config.
    follow_up_seconds: float = Config.get_float("follow_up_listen_seconds", 4.0)
    if follow_up_seconds <= 0:
        return

    # If the server signalled not_for_me (ambient false-wake), skip the
    # follow-up window entirely — otherwise we'd sit listening while the
    # user keeps talking to whoever they were actually addressing.
    # Note: only ``not_for_me`` short-circuits — ``clear_history`` alone
    # is set by many normal one-shot commands (timers, lamp toggles) and
    # those should still allow follow-ups.
    if initial_result and initial_result.get("not_for_me"):
        logger.info("Initial result signalled not_for_me, skipping follow-up loop")
        return

    conversation_id: str | None = None
    if initial_result and initial_result.get("success"):
        conversation_id = initial_result.get("conversation_id")

    # The AudioBus ring buffer is already capturing post-TTS audio. Track
    # when TTS ended so each iteration can ask the bus for "everything
    # since then" via history_secs, capping at the bus's 2s ring capacity.
    # Without this, anything the user said in the gap between TTS-end and
    # listener-attach (~1.5s typical, dominated by barge_in.stop drain)
    # was lost. The caller passes its own measurement of "TTS just ended"
    # because by the time we're called the gap may already be ~1.5s old.
    if tts_end_ts is None:
        tts_end_ts = time.monotonic()
    iteration = 0
    max_iterations: int = Config.get_int("max_follow_up_iterations", MAX_FOLLOW_UP_ITERATIONS)
    consecutive_noise: int = 0
    prev_text: str | None = None
    # Track the last thing the assistant said so the echo check can
    # suppress follow-ups that look like the mic capturing the node's
    # own TTS tail. Seeded from the initial result so the first
    # follow-up iteration is already protected.
    last_assistant_text: str = _extract_assistant_text(initial_result)

    while True:
        iteration += 1

        # Layer 1: Hard cap — prevents infinite loops regardless of audio.
        if iteration > max_iterations:
            logger.info("Follow-up max iterations reached, returning to wake word mode",
                        max_iterations=max_iterations)
            break

        # Layer 2: Decaying timeout — later iterations wait less for onset.
        # Real follow-ups happen quickly; long silences with eventual noise
        # are almost certainly ambient.
        iter_timeout = max(
            FOLLOW_UP_MIN_TIMEOUT,
            follow_up_seconds - (iteration - 1) * FOLLOW_UP_TIMEOUT_DECAY,
        )

        # TTS-tail drain is now handled inside listen_for_follow_up() via
        # the adaptive quiet-wait phase (v0.1.65) — proceed the moment
        # the room is actually quiet instead of guessing a fixed sleep.
        # Replaced the v0.1.64 follow_up_tts_settle_secs hack.
        elapsed = time.monotonic() - tts_end_ts
        history_secs = max(0.0, min(2.0, elapsed))
        logger.info(
            "Follow-up iteration begin",
            iteration=iteration,
            max_iterations=max_iterations,
            elapsed_since_tts=round(elapsed, 3),
            history_secs=round(history_secs, 3),
            timeout=round(iter_timeout, 1),
            consecutive_noise=consecutive_noise,
        )
        # Re-pause any player processes before listening. The conversation's
        # outer pause/restore wrapping (in start_voice_listener) only catches
        # players that existed at wake time — but a previous turn may have
        # *just spawned* a player (e.g. Pandora.play -> mpv) that needs to be
        # silenced for the follow-up capture too.
        #
        # Backgrounded (was synchronous): pkill+pactl cost ~400 ms here on
        # the Pi Zero even when nothing's playing, and it ran BEFORE the
        # listen window started — so 400 ms of every follow-up window was
        # spent on subprocess startup while user audio piled up in the bus.
        # If music IS playing, the pause lands within ~400 ms which is
        # negligible relative to the multi-second listen window.
        _bg_executor.submit(_pause_active_playback)
        # User-facing: signal the listening state on the LED so the user
        # knows the follow-up window is open. Without this the LEDs sit
        # on the idle pattern after the response, the user thinks the
        # system is done, and they don't speak — by the time they do
        # the window has expired. Cleared after listen returns regardless
        # of outcome.
        _set_led_transient("listening")
        try:
            audio_file = listen_for_follow_up(
                bus, timeout_seconds=iter_timeout, history_secs=history_secs,
                follow_up_iteration=iteration,
                silence_threshold=silence_threshold,
                silence_duration=silence_duration,
            )
        finally:
            _set_led_transient(None)
        if audio_file is None:
            logger.info("Follow-up window expired, returning to wake word mode",
                        iteration=iteration)
            break

        # Audio captured — switch to "thinking" so the user knows the
        # system has their input and is processing. Without this the
        # LED returns to idle between speech-detected and TTS-playing,
        # which reads as "it didn't hear me" — leading to re-speaking
        # over the eventual response.
        _set_led_transient("thinking")
        try:
            # Prepend the (cached) wake-word audio for the speaker pass —
            # short follow-ups like "delete it" don't have enough material
            # for ECAPA to score reliably on their own.
            speaker_audio_path = _try_build_speaker_audio(
                wake_audio_path, audio_file,
            )
            transcription_result = stt_provider.transcribe_with_speaker(
                audio_file, speaker_audio_path=speaker_audio_path,
            )
        except Exception as e:
            logger.warning("Follow-up transcription failed", error=str(e))
            _set_led_transient(None)
            break

        if _is_non_speech(transcription_result.text):
            logger.info(
                "Non-speech follow-up transcription, ending follow-up",
                text=transcription_result.text,
            )
            break

        text = transcription_result.text

        # Layer 3: Follow-up noise detection — catches short/repeated
        # transcriptions that slip past _is_non_speech.  Two consecutive
        # noise-like results means the room is noisy, not that the user
        # is talking to us.
        if _is_follow_up_noise(text, prev_text):
            consecutive_noise += 1
            logger.info(
                "Follow-up noise detected",
                text=text, prev_text=prev_text,
                consecutive_noise=consecutive_noise,
                max_consecutive=MAX_CONSECUTIVE_NOISE,
            )
            if consecutive_noise >= MAX_CONSECUTIVE_NOISE:
                logger.info("Too many consecutive noise transcriptions, ending follow-up")
                break
            prev_text = text
            # Don't process noise as a command — immediately re-listen.
            # Update tts_end_ts so next iteration doesn't replay stale audio.
            tts_end_ts = time.monotonic()
            continue

        # Layer 4: Self-echo detection — secondary defense behind the
        # TTS-drain settle at the top of the loop. If the captured
        # transcript ≥85% overlaps the assistant's most recent reply,
        # treat it as the mic re-hearing the TTS tail and exit follow-up
        # rather than feeding it back into CC (the phantom "Here is sad
        # music" loop). Hard exit, not consecutive_noise — a single
        # confirmed echo is enough; we'd rather end follow-up early than
        # risk another fake command.
        if _looks_like_self_echo(text, last_assistant_text):
            logger.info(
                "Follow-up self-echo detected, ending follow-up",
                text=text,
                last_assistant_excerpt=last_assistant_text[:120],
            )
            break

        # Real speech — reset noise counter
        consecutive_noise = 0
        prev_text = text

        speaker_user_id = transcription_result.speaker_user_id
        logger.info("Follow-up speech received", text=text, conversation_id=conversation_id)

        # Start barge-in monitor for TTS playback (if OWW available)
        barge_in: BargeInMonitor | None = None
        if oww and _barge_in_enabled():
            barge_in = BargeInMonitor(
                bus, oww, WAKE_WORD_MODEL,
                threshold=_barge_in_threshold(),
                energy_threshold=_barge_in_energy_threshold(),
            )
            barge_in.start()

        should_break = False
        try:
            # Try pre-routing first (e.g., "stop", "pause")
            pre_result = command_service.try_pre_route(text, conversation_id or "")
            if pre_result is not None:
                command_service.speak_result(pre_result)
                last_assistant_text = _extract_assistant_text(pre_result) or last_assistant_text
                # Pre-routed commands break the CC conversation context
                conversation_id = None
            elif conversation_id:
                # Continue existing conversation
                result = command_service.continue_conversation(
                    conversation_id, text, validation_handler
                )
                command_service.speak_result(result)
                last_assistant_text = _extract_assistant_text(result) or last_assistant_text
                conversation_id = result.get("conversation_id", conversation_id)
                if result.get("clear_history"):
                    logger.info("Conversation complete (clear_history), ending follow-up")
                    should_break = True
            else:
                # No conversation context — start fresh
                result = command_service.process_voice_command(
                    text, validation_handler, speaker_user_id=speaker_user_id
                )
                command_service.speak_result(result)
                last_assistant_text = _extract_assistant_text(result) or last_assistant_text
                conversation_id = result.get("conversation_id") if result.get("success") else None
                if result.get("clear_history"):
                    logger.info("Conversation complete (clear_history), ending follow-up")
                    should_break = True

            # Capture TTS-end timestamp HERE (right after speak_result), not
            # after the finally block. barge_in.stop() routinely takes ~1.5s
            # to drain (oww.reset + thread.join), and that 1.5s is exactly
            # the window the user uses to start their next follow-up. If we
            # mark tts_end_ts after the finally, elapsed≈0 → history_secs≈0
            # → the user's speech in the gap is dropped on iter 2+.
            tts_end_ts = time.monotonic()

        except Exception as e:
            logger.warning("Follow-up processing failed, returning to wake word mode", error=str(e))
            break
        finally:
            if barge_in:
                barge_in.stop()

        if should_break:
            break
        if barge_in and barge_in.was_interrupted:
            logger.info("Barge-in during follow-up, returning to wake word mode")
            platform_audio.reset_cancel()
            break

    # Wake cycle is done — notify CC so it can clear per-node speaker
    # stickiness (and any future lifecycle state). Best-effort: failures
    # are non-fatal, the next wake will just see a stale stickiness entry
    # bounded by the TTL.
    if conversation_id:
        try:
            command_service.client.end_conversation(conversation_id)
        except Exception as e:
            logger.warning("conversation/end raised after follow-up loop", error=str(e))


ALERT_ANNOUNCE_PRIORITY = 3  # Only announce priority >= this (reminders, urgent)
INLINE_LISTEN_TIMEOUT = 8.0  # Seconds to wait for snooze/dismiss after announcement


def _drain_alert_announcements(
    bus: AudioBus,
    command_service: CommandExecutionService,
    stt_provider,
    validation_handler: Callable[[ValidationRequest], str],
) -> bool:
    """Check for high-priority alerts and announce them via TTS.

    Returns True if any announcements were made (caller should reopen stream).
    """
    try:
        queue = get_alert_queue_service()
        pending = queue.get_pending()
    except Exception:
        return False

    # Filter to only high-priority alerts (reminders, urgent emails)
    announcements = [a for a in pending if a.priority >= ALERT_ANNOUNCE_PRIORITY]
    if not announcements:
        return False

    tts_provider = get_tts_provider()

    for alert in announcements:
        logger.info("Announcing alert", title=alert.title, priority=alert.priority)

        # Speak the alert
        try:
            tts_provider.speak(True, alert.summary)
        except Exception as e:
            logger.warning("Alert TTS failed", error=str(e))
            continue

        # Inline listen for response (snooze/dismiss/silence)
        try:
            audio_file = listen_for_follow_up(bus, timeout_seconds=INLINE_LISTEN_TIMEOUT)
            if audio_file is None:
                logger.debug("No response to alert announcement (silence)")
                continue

            transcription_result = stt_provider.transcribe_with_speaker(audio_file)
            if not transcription_result.text:
                continue

            text = transcription_result.text
            speaker_user_id = transcription_result.speaker_user_id
            logger.info("Alert response received", text=text)

            # Process the response (e.g., "snooze", "snooze for 20 minutes", "got it")
            result = command_service.process_voice_command(
                text, validation_handler, speaker_user_id=speaker_user_id,
            )
            command_service.speak_result(result)

        except Exception as e:
            logger.warning("Inline listen after alert failed", error=str(e))

    # Flush the announced alerts from the queue
    try:
        queue.flush()
    except Exception:
        pass

    return True


def _start_keyboard_listener(bus: AudioBus | None = None) -> None:
    """Fallback listener: press Enter to trigger a command (no wake word).

    If ``bus`` is None, a standalone bus is created and started for the
    duration of the session — so this works both as a fallback after
    openwakeword init failure and as an opt-in dev-mode input.
    """
    logger.info("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")
    print("Keyboard mode: press Enter to speak a command, Ctrl+C to quit")

    owns_bus = bus is None
    if bus is None:
        bus = AudioBus(rate=MIC_RATE, chunk_samples=MIC_CHUNK, history_secs=2.0)
        bus.start()

    command_service = CommandExecutionService()
    stt_provider = get_stt_provider()
    validation_handler = _make_validation_handler(bus, stt_provider)

    try:
        while True:
            input()  # block until Enter
            try:
                handle_keyword_detected()
            except Exception as e:
                logger.warning("Wake response TTS failed, continuing", error=str(e))

            # Parallel warmup during recording
            conversation_id = str(uuid.uuid4())
            warmup_result: dict = {"success": False}
            warmup_thread = threading.Thread(
                target=_run_warmup,
                args=(command_service, conversation_id, _last_speaker_user_id, _last_speaker_confidence, warmup_result),
                daemon=True,
            )
            warmup_thread.start()

            recording = listen(bus, history_secs=0.0)

            ack_played = _play_processing_ack()

            start = time.perf_counter()
            result = send_for_transcription(
                recording, command_service, stt_provider, validation_handler,
                warmup_thread=warmup_thread,
                conversation_id=conversation_id,
                warmup_result=warmup_result,
                skip_ack=ack_played,
            )
            tts_end_ts = time.monotonic()
            end = time.perf_counter()

            logger.info("Transcription complete", duration_seconds=round(end - start, 2))

            _follow_up_loop(bus, result, command_service, stt_provider, validation_handler, tts_end_ts=tts_end_ts)

            # Pre-generate the next processing ack in the background
            _bg_executor.submit(_fetch_next_processing_ack)

            logger.info("Press Enter to speak another command")
    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        if owns_bus:
            bus.stop()


def start_voice_listener(ma_service):
    """Main voice loop: wake → respond → listen → process → follow-up.

    One long-running AudioBus owns the mic for the lifetime of the node.
    Every audio consumer (wake detector, barge-in, command listen,
    follow-up) subscribes to the bus instead of opening its own PyAudio
    stream. This eliminates the concurrent-dsnoop-open race that caused
    BLANK_AUDIO captures in the pre-AudioBus implementation.

    Flow per iteration:
      1. Subscribe ``wake`` on the bus (48 kHz chunks).
      2. Downsample each chunk to 16 kHz and score with openWakeWord.
      3. On wake, unsubscribe ``wake`` so the wake detector doesn't
         fight the command listener for the queue.
      4. Play wake response (blocking TTS).
      5. Record the command via ``listen(bus, history_secs=0.0)`` —
         quiet-wait inside listen() drains the wake-response TTS tail —
         the 0.3s skip dodges TTS-tail bleed / AEC recovery without
         replaying the tail of the wake response into the recording
         (which would otherwise cause the node to transcribe its own
         TTS and respond to itself — "talking to itself" bug).
      6. Start barge-in monitor (also a bus subscriber) during
         STT → CC → TTS response playback.
      7. On barge-in OR normal completion, run the follow-up loop.
      8. Back to step 1 with a fresh ``wake`` subscription.
    """
    try:
        openwakeword.utils.download_models(model_names=[WAKE_WORD_MODEL])
        oww = OWWModel(wakeword_models=[WAKE_WORD_MODEL], inference_framework="onnx")
    except Exception as e:
        logger.warning("openWakeWord init failed, falling back to keyboard trigger", error=str(e))
        if sys.stdin and sys.stdin.isatty():
            _start_keyboard_listener()
        else:
            logger.error("No TTY available for keyboard fallback, exiting")
        return

    # Retry bus start — USB mic may not be ready immediately after boot.
    _audio_retry_delays: list[int] = [2, 2, 5, 5, 10, 10, 15, 15, 30, 30, 30, 30]
    bus: AudioBus | None = None
    for attempt, delay in enumerate(_audio_retry_delays):
        try:
            bus = AudioBus(rate=MIC_RATE, chunk_samples=MIC_CHUNK, history_secs=2.0)
            bus.start()
            break
        except OSError as e:
            logger.warning("Audio device unavailable",
                           error=str(e), attempt=attempt + 1,
                           max_attempts=len(_audio_retry_delays),
                           retry_in_seconds=delay)
            if bus is not None:
                try:
                    bus.stop()
                except Exception:
                    pass
                bus = None
            time.sleep(delay)

    if bus is None:
        logger.error("No audio device found after retries, giving up",
                     total_attempts=len(_audio_retry_delays))
        return

    # Expose the running bus to other subsystems that need to consume mic
    # audio (e.g. MQTT-triggered voice enrollment).
    global _audio_bus
    _audio_bus = bus

    command_service = CommandExecutionService()
    stt_provider = get_stt_provider()
    validation_handler = _make_validation_handler(bus, stt_provider)

    # Pre-warm the LLM's KV cache and processing ack on boot.
    _bg_executor.submit(_run_warmup, command_service, str(uuid.uuid4()), None, {})
    _bg_executor.submit(_fetch_next_processing_ack)

    logger.info("Waiting for wake word", model=WAKE_WORD_MODEL,
                threshold=_wake_threshold())
    print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}' (threshold={_wake_threshold()})")

    resample_down = bus.rate // OWW_RATE  # 3 for 48 kHz → 16 kHz
    alert_check_interval = 60             # ~every 5s at 80 ms chunks
    alert_check_counter = 0

    # Inline AEC: pulls a reference frame from PA's monitor source per
    # mic chunk, subtracts speaker bleed via Speex. Disabled by default
    # (gated on aec_enabled); any startup failure falls back to a
    # passthrough so wake detection stays alive.
    aec_pipeline: AecPipeline | None = None
    if Config.get_bool("aec_enabled", False):
        try:
            aec_filter_ms = Config.get_float("aec_filter_length_ms", 100.0)
            aec_delay_ms = Config.get_float("aec_reference_delay_ms", 80.0)
            aec_pipeline = AecPipeline(
                rate=OWW_RATE,
                frame_size=160,
                filter_length=int(aec_filter_ms * OWW_RATE / 1000),
                reference_delay_samples=int(aec_delay_ms * OWW_RATE / 1000),
                reference_buffer_secs=Config.get_float("aec_buffer_secs", 2.0),
                monitor_source=Config.get_str("aec_monitor_source"),
            )
            aec_pipeline.start()
            if Config.get_bool("aec_calibrate_on_startup", True):
                try:
                    aec_pipeline.calibrate_delay(bus)
                except Exception as e:
                    logger.warning("AEC calibration raised; keeping configured delay", error=str(e))
        except Exception as e:
            logger.error("AEC pipeline init failed, continuing without AEC", error=str(e))
            aec_pipeline = None

    try:
        while True:
            # Safety net: ensure no stale cancel state from a prior
            # barge-in prevents wake-response or other audio.
            platform_audio.reset_cancel()

            # Re-read the wake threshold each outer iteration so mobile-app
            # updates apply without a service restart. Using a local also
            # keeps the inner loop hot path off the disk.
            wake_threshold = _wake_threshold()
            # Snapshot music state alongside the threshold so the energy
            # gate below matches the threshold's assumption. _music_is_playing
            # round-trips to pactl (~ms) so we don't want to call it per
            # chunk; we only need it to be coherent with the threshold
            # used for the current outer iteration.
            #
            # Secondary check via AEC's reference reader: pactl detection
            # is fragile (misses stuck playback after a failed stop-music
            # command, briefly-corked sink-inputs, unrecognized player
            # binaries). ref_rms reflects what's actually being sent to
            # the speaker, so it catches the cases pactl misses.
            music_mode = _music_is_playing() or (
                aec_pipeline is not None
                and aec_pipeline.has_recent_reference_signal()
            )
            music_energy_multiplier = _wake_music_energy_multiplier()

            wake_q = bus.subscribe("wake")
            score = 0.0
            # Pre-wake VAD ring buffer — one bool per 80 ms chunk, last
            # PRE_WAKE_VAD_FRAMES kept. On wake fire we sum it and report
            # how many seconds of speech happened in the window before wake.
            # Fresh per outer iteration so prior interactions don't leak in.
            pre_wake_speech_frames: deque[bool] = deque(maxlen=PRE_WAKE_VAD_FRAMES)
            # Parallel RMS-value deque used only for the wake-fire diagnostic
            # log — lets us see the actual mic baseline so the threshold can
            # be tuned per room without guessing.
            pre_wake_rms_values: deque[float] = deque(maxlen=PRE_WAKE_VAD_FRAMES)
            pre_wake_vad_threshold: float = _pre_wake_vad_threshold()
            pre_wake_speech_seconds: float | None = None
            try:
                was_paused = False
                while True:
                    alert_check_counter += 1
                    if alert_check_counter >= alert_check_interval:
                        alert_check_counter = 0
                        try:
                            aq = get_alert_queue_service()
                            has_announcements = any(
                                a.priority >= ALERT_ANNOUNCE_PRIORITY
                                for a in aq.get_pending()
                            )
                        except Exception:
                            has_announcements = False
                        if has_announcements:
                            # Let the alert drain run — it uses the bus too.
                            break  # ← exits inner loop with score==0; see below

                    try:
                        raw_data = wake_q.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    # While paused, drop the chunk and don't score it. The
                    # queue still drains so we don't process stale audio
                    # the moment we resume.
                    if _wake_paused.is_set():
                        was_paused = True
                        continue

                    # First chunk after a pause: reset the openWakeWord LSTM
                    # state. Without this, residual context from before the
                    # pause (often the wake response audio echoing back)
                    # immediately re-triggers a wake event. Also drop the
                    # pre-wake VAD buffer — frames from before the pause
                    # are no longer "the room before this wake".
                    if was_paused:
                        with _oww_lock:
                            oww.reset()
                        pre_wake_speech_frames.clear()
                        pre_wake_rms_values.clear()
                        was_paused = False

                    samples = np.frombuffer(raw_data, dtype=np.int16)

                    # Per-chunk RMS for the pre-wake VAD ring buffer. Use
                    # the raw 48 kHz samples (before resample/clip) so the
                    # energy reading is unmodified mic input. Tiny cost.
                    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
                    pre_wake_speech_frames.append(rms > pre_wake_vad_threshold)
                    pre_wake_rms_values.append(rms)

                    if resample_down > 1:
                        resampled = _get_resample_poly()(samples, up=1, down=resample_down)
                        samples = np.clip(resampled, -32768, 32767).astype(np.int16)

                    if aec_pipeline is not None:
                        samples = aec_pipeline.process(samples)
                        # The pre-wake RMS readings (used by the music
                        # energy gate below) were taken from the raw mic
                        # above — that signal is still dominated by music
                        # bleed even when AEC has cleaned the voice into
                        # a strong OWW score. Re-measure on the cleaned
                        # signal so the gate's baseline and current-frame
                        # RMS are both post-cancellation and comparable.
                        # Without this, AEC-cleaned wakes score 0.5-0.8
                        # but get suppressed because raw mic bleed > raw
                        # mic-with-voice in RMS terms.
                        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
                        pre_wake_rms_values[-1] = rms

                    with _oww_lock:
                        predictions = oww.predict(samples)
                    score = predictions.get(WAKE_WORD_MODEL, 0)
                    # Per-chunk predict timing logging was removed
                    # 2026-05-20: on the Pi Zero, logger.info() costs
                    # 5-20 ms per call, and our >60 ms warning was
                    # firing every chunk in steady state — creating a
                    # feedback loop where the logging itself pushed
                    # predict from 60 ms toward 80 ms, then logged
                    # again. We measured predict at 60-80 ms (right at
                    # the 80 ms real-time budget) — that's a known
                    # property of openWakeWord on this hardware, not
                    # something logging per chunk can help with.
                    # Per-frame re-check: music_mode set at outer-loop entry
                    # goes stale the moment playback starts/stops mid-iteration.
                    # AEC's ref_rms window tracks the speaker sink in real time;
                    # if it sees signal NOW, flip into music_mode and use the
                    # lower music threshold even though the outer-loop value
                    # was False. This is what unsticks "music kept playing
                    # after a failed stop command — and now I can't wake it".
                    effective_music_mode = music_mode or (
                        aec_pipeline is not None
                        and aec_pipeline.has_recent_reference_signal()
                    )
                    effective_threshold = (
                        Config.get_float("wake_word_threshold_music", 0.12)
                        if effective_music_mode
                        else wake_threshold
                    )
                    if score > 0.05:
                        # Bounded: only fires when oww sees something
                        # meaningful (someone actually talking). Useful
                        # for debugging "I said hey jarvis but it didn't
                        # hear" — shows the score it saw vs threshold.
                        logger.info(
                            "oww-score",
                            score=round(float(score), 3),
                            threshold=effective_threshold,
                            music_mode=effective_music_mode,
                        )
                    fire_wake = score > effective_threshold
                    if fire_wake and effective_music_mode:
                        # If AEC is consistently cancelling well, the
                        # post-cancellation OWW score is the right signal
                        # to trust — the energy gate (designed pre-AEC)
                        # becomes redundant and just adds a failure mode.
                        # The gate stays in place whenever AEC is weak or
                        # unavailable (no playback path, low suppression,
                        # adaptation still ramping).
                        aec_trusted = (
                            aec_pipeline is not None
                            and aec_pipeline.is_strongly_active(
                                threshold_db=Config.get_float(
                                    "aec_trust_threshold_db", 5.0
                                ),
                            )
                        )
                        if aec_trusted:
                            if score > 0.4:
                                logger.info(
                                    "wake-aec-trusted-skip-gate",
                                    score=round(float(score), 3),
                                    suppression_db=round(
                                        aec_pipeline.recent_suppression_db() or 0.0, 1
                                    ),
                                )
                        elif len(pre_wake_rms_values) >= 6:
                            # Music-mode energy gate: require current RMS to
                            # spike above the running baseline (voice ON TOP
                            # of music). Without this, the lowered music
                            # threshold (~0.12) trips on music alone — the
                            # OWW model regularly hits 0.10-0.18 against
                            # speaker bleed even when nobody's speaking.
                            # Mirrors barge_in's two-tier (low OWW + energy
                            # above baseline) approach.
                            sorted_rms = sorted(pre_wake_rms_values)
                            baseline_rms = sorted_rms[len(sorted_rms) // 2]
                            energy_floor = baseline_rms * music_energy_multiplier
                            if rms <= energy_floor:
                                fire_wake = False
                                if score > 0.08:
                                    logger.info(
                                        "wake-suppressed-music-bleed",
                                        score=round(float(score), 3),
                                        rms=round(rms, 1),
                                        baseline_rms=round(baseline_rms, 1),
                                        energy_floor=round(energy_floor, 1),
                                    )
                        else:
                            # Not enough history yet — be conservative
                            # and don't fire on the music-mode threshold
                            # until we've sampled ~480 ms of baseline.
                            fire_wake = False
                    if fire_wake:
                        t_wake_fired = time.monotonic()
                        # Snapshot the score for the auto-calibrator. The
                        # ``score`` variable will be overwritten when the
                        # outer loop iterates again; we need it later
                        # (after CC's not_for_me verdict) to decide whether
                        # this wake counts as a "legitimate" data point.
                        score_at_wake = float(score)
                        speech_frames = sum(pre_wake_speech_frames)
                        pre_wake_speech_seconds = round(
                            speech_frames * _CHUNK_SECONDS, 2,
                        )
                        # Diagnostic RMS stats for threshold calibration.
                        if pre_wake_rms_values:
                            rms_sorted = sorted(pre_wake_rms_values)
                            rms_n = len(rms_sorted)
                            rms_stats = {
                                "max": round(rms_sorted[-1], 1),
                                "p95": round(rms_sorted[int(rms_n * 0.95)], 1),
                                "p75": round(rms_sorted[int(rms_n * 0.75)], 1),
                                "median": round(rms_sorted[rms_n // 2], 1),
                                "min": round(rms_sorted[0], 1),
                            }
                        else:
                            rms_stats = {}
                        logger.info(
                            "Wake fired",
                            score=round(float(score), 3),
                            pre_wake_speech_seconds=pre_wake_speech_seconds,
                            pre_wake_window_seconds=PRE_WAKE_VAD_WINDOW_SECS,
                            buffered_frames=len(pre_wake_speech_frames),
                            vad_threshold=pre_wake_vad_threshold,
                            rms_stats=rms_stats,
                        )
                        # Lock the per-cycle silence threshold to the ambient
                        # noise floor observed RIGHT before wake. Used below
                        # for the command listen() (and follow-up listens),
                        # so a static config value can't be wrong for the
                        # current room state.
                        adaptive_silence_threshold = _adaptive_silence_threshold(rms_stats)
                        if adaptive_silence_threshold is not None:
                            logger.info(
                                "Adaptive silence threshold",
                                silence_threshold=adaptive_silence_threshold,
                                ambient_median_rms=rms_stats.get("median"),
                            )
                        break
            finally:
                _t_unsub_start = time.monotonic()
                bus.unsubscribe("wake")
                logger.info(
                    f"⏱️ wake-step | bus.unsubscribe took "
                    f"{int((time.monotonic() - _t_unsub_start) * 1000)}ms"
                )

            # If we broke out without a wake (alert-drain case), handle
            # alerts and loop.
            if score <= wake_threshold:
                try:
                    _drain_alert_announcements(
                        bus, command_service, stt_provider, validation_handler,
                    )
                except Exception as e:
                    logger.warning("Alert drain failed", error=str(e))
                with _oww_lock:
                    oww.reset()
                print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}'")
                continue

            # Clear oww's LSTM state so the next wake cycle doesn't
            # false-retrigger on the tail of the wake word we just
            # detected — BUT do it in the background. On the Pi Zero
            # ``oww.reset()`` is ~1.5 s of model state reinit, and
            # blocking on it here pushes "You rang?" 1.5 s later for
            # zero user benefit (the next wake loop won't run for
            # multiple seconds while we play TTS, listen, etc.). The
            # _oww_lock guarantees the reset finishes before any future
            # predict() runs.
            _t_reset_start = time.monotonic()
            _bg_executor.submit(_locked_oww_reset, oww)
            logger.info(
                f"⏱️ wake-step | oww.reset SUBMITTED to background in "
                f"{int((time.monotonic() - _t_reset_start) * 1000)}ms"
            )

            # Drop music volume for the duration of the command exchange so
            # the user's voice isn't competing with their own playback.
            # Fire-and-forget background — see note above the executor.
            _t_duck_start = time.monotonic()
            _bg_executor.submit(_duck_music)
            logger.info(
                f"⏱️ wake-step | duck submit took "
                f"{int((time.monotonic() - _t_duck_start) * 1000)}ms"
            )

            # Snapshot the wake-word audio from the bus *now* — before
            # handle_keyword_detected plays the TTS ack which can take
            # 500-1500ms. The bus only holds ~2s of history so any later
            # snapshot risks falling outside that window. The same
            # snapshot is reused for every follow-up in this conversation.
            wake_audio_path = _try_capture_wake_audio(bus)

            try:
                _t_handle_start = time.monotonic()
                logger.info(
                    f"⏱️ wake-step | T+"
                    f"{int((_t_handle_start - t_wake_fired) * 1000)}ms "
                    f"entering handle_keyword_detected"
                )
                try:
                    handle_keyword_detected()
                except Exception as e:
                    logger.warning("Wake response TTS failed, continuing", error=str(e))
                _t_handle_end = time.monotonic()
                logger.info(
                    f"⏱️ wake-step | handle_keyword_detected took "
                    f"{int((_t_handle_end - _t_handle_start) * 1000)}ms "
                    f"(T+{int((_t_handle_end - t_wake_fired) * 1000)}ms total)"
                )

                conversation_id = str(uuid.uuid4())
                warmup_result: dict = {"success": False}
                warmup_thread = threading.Thread(
                    target=_run_warmup,
                    args=(command_service, conversation_id, _last_speaker_user_id, _last_speaker_confidence, warmup_result),
                    daemon=True,
                )
                warmup_thread.start()

                barge_in: BargeInMonitor | None = None
                if _barge_in_enabled():
                    barge_in = BargeInMonitor(
                        bus, oww, WAKE_WORD_MODEL,
                        threshold=_barge_in_threshold(),
                        energy_threshold=_barge_in_energy_threshold(),
                    )

                result = None
                _t_listen_start = time.monotonic()
                try:
                    # history_secs=0 + adaptive quiet-wait (v0.1.65) inside
                    # listen(): do NOT replay the wake-response TTS tail
                    # (that bug made the node transcribe and respond to
                    # itself). Quiet-wait drains the speaker tail per-room.
                    #
                    # silence_threshold passed explicitly when adaptive mode
                    # produced one — calibrated to the room's ambient floor
                    # at the moment of wake (see _adaptive_silence_threshold).
                    # When None, listen() falls back to its static config
                    # value, preserving prior behavior.
                    #
                    # quiet_wait gated on actual speaker activity: with
                    # wake_ack_audio_enabled=False AND no music playing,
                    # the speaker is silent — running the drain would
                    # misidentify the user's own command (4000+ RMS) as
                    # speaker echo and discard the first 400 ms of speech.
                    # That's the "cut off mid-sentence" pattern from prod.
                    # AEC reference-signal is the right signal: True only
                    # when the speaker actually had recent output.
                    speaker_recently_active = (
                        aec_pipeline is not None
                        and aec_pipeline.has_recent_reference_signal()
                    )
                    listen_quiet_wait_secs = None if speaker_recently_active else 0.0
                    # silence_duration override (default 0.8 s): natural
                    # mid-sentence pauses (between clauses, taking a breath)
                    # commonly run 250-400 ms. The historical 0.3 s default
                    # was chopping multi-clause commands. 0.8 s tolerates a
                    # normal pause without dragging out silence detection on
                    # actual end-of-utterance.
                    listen_silence_duration = Config.get_float(
                        "listen_silence_duration_secs", 0.8,
                    )
                    recording = listen(
                        bus, history_secs=0.0,
                        silence_threshold=adaptive_silence_threshold,
                        silence_duration=listen_silence_duration,
                        quiet_wait_secs=listen_quiet_wait_secs,
                    )
                    _t_listen_end = time.monotonic()
                    logger.info(
                        f"⏱️ wake-step | listen() took "
                        f"{int((_t_listen_end - _t_listen_start) * 1000)}ms "
                        f"(T+{int((_t_listen_end - t_wake_fired) * 1000)}ms total)"
                    )

                    # Suppress the post-listen processing-ack when audio
                    # acks are disabled — the deferred wake-ack (played by
                    # on_llm_fallback inside process_voice_command) covers
                    # the same job without stacking two back-to-back acks.
                    if Config.get_bool("wake_ack_audio_enabled", True):
                        _t_ack_start = time.monotonic()
                        ack_played = _play_processing_ack()
                        logger.info(
                            f"⏱️ wake-step | processing-ack took "
                            f"{int((time.monotonic() - _t_ack_start) * 1000)}ms "
                            f"(played={ack_played})"
                        )
                    else:
                        ack_played = False

                    if barge_in:
                        barge_in.start()

                    _t_xform_start = time.monotonic()
                    result = send_for_transcription(
                        recording, command_service, stt_provider, validation_handler,
                        warmup_thread=warmup_thread,
                        conversation_id=conversation_id,
                        warmup_result=warmup_result,
                        skip_ack=ack_played,
                        pre_wake_speech_seconds=pre_wake_speech_seconds,
                        wake_audio_path=wake_audio_path,
                    )
                    # Feed the auto-calibrator: wake scores that produced a
                    # real interaction (not the not_for_me silent abort)
                    # become anchors for the threshold. Failures are NOT
                    # recorded, biasing the calibrator toward conservatism
                    # — a wake that CC rejected might have been a real
                    # false-positive and including it would lower the bar
                    # for genuine false positives.
                    if isinstance(result, dict) and not result.get("not_for_me"):
                        _record_legitimate_wake_score(score_at_wake)
                    # Capture TTS-end time RIGHT after send_for_transcription
                    # returns (which is right after speak_result completes).
                    # The follow-up loop uses this to know how far back to look
                    # in the bus history for speech the user uttered before the
                    # listener subscribed. barge_in.stop() in the finally below
                    # routinely costs ~1.5s — that 1.5s is exactly the gap users
                    # speak into.
                    tts_end_ts = time.monotonic()
                    _t_xform_end = tts_end_ts
                    logger.info(
                        f"⏱️ wake-step | send_for_transcription took "
                        f"{int((_t_xform_end - _t_xform_start) * 1000)}ms "
                        f"(T+{int((_t_xform_end - t_wake_fired) * 1000)}ms total "
                        f"— incl STT + CC + TTS playback)"
                    )
                except Exception as e:
                    logger.warning("Command processing failed, resuming listener", error=str(e))
                    print(f"Command failed: {e}")
                    tts_end_ts = time.monotonic()
                finally:
                    _t_barge_stop_start = time.monotonic()
                    if barge_in:
                        barge_in.stop()
                    logger.info(
                        f"⏱️ wake-step | barge_in.stop took "
                        f"{int((time.monotonic() - _t_barge_stop_start) * 1000)}ms"
                    )

                if barge_in and barge_in.was_interrupted:
                    logger.info("Barge-in: TTS interrupted, returning to wake word")
                    platform_audio.reset_cancel()
                    # Don't try to capture a new command here — the user
                    # interrupted to STOP the response.  They'll say the
                    # wake word again when they're ready.
                else:
                    try:
                        _follow_up_loop(
                            bus, result, command_service, stt_provider, validation_handler,
                            oww=oww, tts_end_ts=tts_end_ts,
                            wake_audio_path=wake_audio_path,
                            silence_threshold=adaptive_silence_threshold,
                            silence_duration=listen_silence_duration,
                        )
                    except Exception as e:
                        logger.warning("Follow-up loop error, resuming wake word", error=str(e))
            finally:
                # Mirror the duck: always background, no pre-check. The
                # restore (pactl unmute + SIGCONT) is idempotent against
                # missing/unmuted targets, so spending background CPU when
                # nothing was paused is harmless. Critical: never block
                # the return to wake-word mode on pactl round-trips.
                _bg_executor.submit(_restore_music)
                # Safety net: always clear the transient LED state when
                # returning to wake-word mode, so a half-finished path can't
                # leave the pinwheel (or any other transient) stuck.
                _set_led_transient(None)

            logger.info(
                f"⏱️ wake-cycle COMPLETE | total="
                f"{int((time.monotonic() - t_wake_fired) * 1000)}ms "
                f"from wake_fired to return-to-idle"
            )
            _bg_executor.submit(_fetch_next_processing_ack)
            print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}'")

    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        if aec_pipeline is not None:
            aec_pipeline.stop()
        bus.stop()
        pa.terminate()
        del oww
