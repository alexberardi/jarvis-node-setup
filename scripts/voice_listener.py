"""Voice listener entry point.

Owns module-level wake-control state (``pause_wake`` / ``resume_wake`` /
``wake_paused``), the bounded background executor used across the wake
path, the keyboard-trigger fallback, and the ``start_voice_listener``
entry point that wires up the openWakeWord model, the long-running
AudioBus, the optional AEC pipeline, command/STT/validation services,
then hands control to :func:`core.wake_loop.run_wake_loop`.

External consumers:
  * ``scripts.main`` imports :func:`start_voice_listener`.
  * ``scripts.mqtt_tts_listener`` imports :func:`get_audio_bus` +
    :func:`wake_paused` for voice-enrollment-via-MQTT, which subscribes
    to the running bus while wake detection is held off.
"""

import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Iterator

import openwakeword
from openwakeword.model import Model as OWWModel

from jarvis_log_client import JarvisLogger

from core.aec_pipeline import AecPipeline
from core.audio_bus import AudioBus
from core.helpers import get_stt_provider
from core import voice_filters  # noqa: F401  re-export for callers
from core import follow_up_loop as _follow_up_module
from core import wake_loop
from core import wake_response
from core.follow_up_loop import follow_up_loop
from core.wake_detector import current_wake_threshold
from core.wake_loop import run_wake_loop
from core.wake_response import (
    fetch_next_processing_ack,
    handle_keyword_detected,
    play_processing_ack,
    run_warmup,
)
from core.wake_transcription import (
    get_last_speaker,
    make_validation_handler,
    send_for_transcription,
)
from scripts.speech_to_text import listen
from utils.config_service import Config
from utils.command_execution_service import CommandExecutionService


logger = JarvisLogger(service="jarvis-node")

# Bounded pool for fire-and-forget background tasks (wake response fetch,
# processing ack generation, audio playback).  Prevents thread leaks — bare
# threading.Thread() calls were leaving 1-2 orphan threads per voice command.
_bg_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="voice-bg")

# follow_up_loop needs the same bounded executor so its pre-listen
# music-pause submit lands on the same pool (no thread leaks). Wired
# now that the executor exists; tests don't see this — they monkeypatch
# the module attribute directly.
_follow_up_module.set_runtime(bg_executor=_bg_executor)

# wake_word_model is baked into the openWakeWord instance loaded at startup,
# so changing it still requires a service restart. Everything else below is
# read fresh on each wake / follow-up cycle so mobile-app updates apply live.
WAKE_WORD_MODEL = Config.get_str("wake_word_model", "hey_jarvis") or "hey_jarvis"


# openWakeWord rate / chunk shape, and the matching capture rates for
# common USB mics. The wake loop downsamples 48 kHz → 16 kHz per chunk.
OWW_RATE = 16000
OWW_CHUNK = 1280
# 48 kHz is the ReSpeaker HAT's native rate; USB mics that only support
# 44.1 kHz (or other rates) override via the `mic_sample_rate` setting —
# wake_loop resamples whatever we capture down to OWW_RATE via a rational
# ratio, so the rate no longer has to be a multiple of 16 kHz.
MIC_RATE = Config.get_int("mic_sample_rate", 48000)
MIC_CHUNK = round(OWW_CHUNK * MIC_RATE / OWW_RATE)  # 80 ms at MIC_RATE


# Shared AudioBus, set when start_voice_listener() initializes its bus.
# Other subsystems (e.g. enrollment-via-MQTT) need a way to consume mic
# audio without opening a competing PyAudio stream — they call
# ``get_audio_bus()`` and subscribe.
_audio_bus: AudioBus | None = None


def get_audio_bus() -> AudioBus | None:
    """Return the running AudioBus, or None if voice_listener hasn't started."""
    return _audio_bus


# When set, the main wake loop short-circuits its score check and yields
# the CPU. Used by transient flows that want to consume the mic via the
# bus without competing with wake detection — voice-profile enrollment
# in particular, where reading a sample prompt aloud near the mic would
# otherwise re-fire the wake detector and clash with the recording.
_wake_paused = threading.Event()


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


# Inject runtime deps into wake_response and wake_loop now that the
# executor and the wake_paused primitive both exist. wake_response wants
# the context-manager factory (it pauses wake detection while its own
# audio plays back); wake_loop wants the Event directly so the inner
# chunk-pulling loop can check it without taking a function call.
# Tests don't see these calls — they monkeypatch the module attributes
# directly.
wake_response.set_runtime(bg_executor=_bg_executor, wake_paused_factory=wake_paused)
wake_loop.set_runtime(bg_executor=_bg_executor, wake_paused=_wake_paused)


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
    validation_handler = make_validation_handler(bus, stt_provider)

    try:
        while True:
            input()  # block until Enter
            try:
                try:
                    handle_keyword_detected()
                except Exception as e:
                    logger.warning("Wake response TTS failed, continuing", error=str(e))

                # Parallel warmup during recording
                conversation_id = str(uuid.uuid4())
                warmup_result: dict = {"success": False}
                warmup_thread = threading.Thread(
                    target=run_warmup,
                    args=(command_service, conversation_id, *get_last_speaker(), warmup_result),
                    daemon=True,
                )
                warmup_thread.start()

                recording = listen(bus, history_secs=0.0)

                ack_played = play_processing_ack()

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

                follow_up_loop(bus, result, command_service, stt_provider, validation_handler, wake_word_model=WAKE_WORD_MODEL, tts_end_ts=tts_end_ts)

                # Pre-generate the next processing ack in the background
                _bg_executor.submit(fetch_next_processing_ack)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                # A mic/STT/CC failure must not kill keyboard mode — and
                # must not strand the transient LED (see finally).
                logger.error("Keyboard-mode command failed, continuing", error=str(e))
            finally:
                # Mirror run_wake_loop's safety net: handle_keyword_detected
                # sets the purple "wake_detected" transient, and an exception
                # anywhere downstream would otherwise leave all three LEDs
                # purple indefinitely.
                wake_response.set_led_transient(None)

            logger.info("Press Enter to speak another command")
    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        if owns_bus:
            bus.stop()


def start_voice_listener(ma_service):
    """Wake-loop entry point.

    Sets up the openWakeWord model, the long-running AudioBus, services
    (command / STT / validation), pre-warms the LLM and processing-ack
    cache, optionally starts the inline AEC pipeline, then hands the
    fully-initialized resources to :func:`core.wake_loop.run_wake_loop`.

    KeyboardInterrupt cleanup (stopping the AEC pipeline, stopping the
    bus, releasing the OWW model) stays here — the loop body doesn't
    need to know about resources it didn't create.
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
    validation_handler = make_validation_handler(bus, stt_provider)

    # Pre-warm the LLM's KV cache and processing ack on boot.
    _bg_executor.submit(run_warmup, command_service, str(uuid.uuid4()), None, {})
    _bg_executor.submit(fetch_next_processing_ack)

    logger.info("Waiting for wake word", model=WAKE_WORD_MODEL,
                threshold=current_wake_threshold())
    print(f"Ready — say '{WAKE_WORD_MODEL.replace('_', ' ')}' (threshold={current_wake_threshold()})")

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
        run_wake_loop(
            bus=bus,
            oww=oww,
            command_service=command_service,
            stt_provider=stt_provider,
            validation_handler=validation_handler,
            aec_pipeline=aec_pipeline,
            wake_word_model=WAKE_WORD_MODEL,
        )
    except KeyboardInterrupt:
        logger.info("Stopping voice listener")
    finally:
        if aec_pipeline is not None:
            aec_pipeline.stop()
        bus.stop()
        del oww
