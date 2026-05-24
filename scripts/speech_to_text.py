"""Command-capture primitives over an AudioBus.

These are pure functions: they subscribe to the bus, consume chunks
until a stopping condition (silence, speech-onset, timeout), write a
WAV to the cache dir, and return a RecordingResult. They do not open
PyAudio. They do not resolve devices. The bus owns the mic; we just
read from it.

The old `skip_frames` hack (discarding the first 0.3s/1.0s of a fresh
PyAudio open to dodge TTS bleed) is gone. Its job is now done by
subscribing with ``history_secs`` at the state-machine layer — the
first second of "recording" is replayed from the ring buffer, so
audio the user emitted during the tail of TTS playback is captured
rather than discarded.
"""

from __future__ import annotations

import queue
import time
import wave
from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pyaudio

from jarvis_log_client import JarvisLogger

from core.audio_bus import AudioBus
from utils.config_service import Config
from utils.encryption_utils import get_cache_dir


@dataclass
class RecordingResult:
    """Metadata from a recording session."""
    audio_file: str          # path to the WAV file
    duration: float          # actual recording duration in seconds
    hit_max_duration: bool   # True if recording stopped due to max_record_seconds


logger = JarvisLogger(service="jarvis-node")
_cache_dir = get_cache_dir()


def _audio_defaults() -> dict:
    """Live read of audio-related config values."""
    return {
        "silence_threshold": Config.get_int("silence_threshold", 300),
        "silence_duration": Config.get_float("silence_duration", 1.5),
        "min_record_seconds": Config.get_float("min_record_seconds", 2.0),
        "max_record_seconds": Config.get_int("max_record_seconds", 7),
    }


def calculate_rms(audio_data: bytes) -> float:
    """RMS of a 16-bit PCM chunk. Returns 0.0 on empty or NaN."""
    if not audio_data:
        return 0.0
    audio_array = np.frombuffer(audio_data, dtype=np.int16)
    if len(audio_array) == 0:
        return 0.0
    rms = np.sqrt(np.mean(audio_array.astype(np.float32) ** 2))
    if np.isnan(rms):
        return 0.0
    return float(rms)


def _drain_until_quiet(
    q: "queue.Queue[bytes]",
    *,
    chunk_secs: float,
    max_wait_secs: float,
    quiet_rms_threshold: float,
    consecutive_quiet_secs: float,
) -> tuple[int, float, bool]:
    """Pop frames off ``q`` until the room has been quiet for
    ``consecutive_quiet_secs``, OR ``max_wait_secs`` has elapsed.

    Replaces the v0.1.64-and-earlier fixed-time ``skip_secs`` hack —
    instead of guessing 0.3 s and hoping the room is quiet by then, we
    watch the mic and proceed the moment it actually goes quiet. The
    kitchen-node beta-test reproduced 400-700 ms of TTS tail bleeding
    into the listen window through cabinet reverb; this loop adapts to
    that per-room without any tuning knob.

    Returns ``(frames_drained, max_rms_observed, reached_quiet)`` for
    diagnostics. Frames are intentionally discarded — they're the TTS
    tail we wanted to skip.
    """
    needed_quiet_frames = max(1, int(consecutive_quiet_secs / chunk_secs))
    max_frames = max(1, int(max_wait_secs / chunk_secs))
    consecutive_below = 0
    drained = 0
    max_rms = 0.0
    reached_quiet = False
    for _ in range(max_frames):
        try:
            data = q.get(timeout=max(chunk_secs * 10, 1.0))
        except queue.Empty:
            break
        drained += 1
        rms = calculate_rms(data)
        if rms > max_rms:
            max_rms = rms
        if rms < quiet_rms_threshold:
            consecutive_below += 1
            if consecutive_below >= needed_quiet_frames:
                reached_quiet = True
                break
        else:
            consecutive_below = 0
    return drained, max_rms, reached_quiet


def _normalize_frames_to_target_rms(
    frames: List[bytes],
    *,
    target_rms_db: float,
    max_gain_db: float,
    min_gain_db: float = 0.5,
) -> tuple[List[bytes], float, float]:
    """Apply a single static gain so the joined AC-RMS sits near ``target_rms_db``.

    Whisper performs best on speech at roughly -18 to -14 dB FS. Faint
    far-field kitchen-mic recordings come in at -25 to -30 dB FS and
    the model starts hallucinating to fill the dynamic-range gap (the
    May-2026 beta saw "play kiss from a rose" transcribed as "Thank
    you for our rooms"). Bringing the captured audio up to Whisper's
    sweet spot reduces the model's freedom to hallucinate without
    altering the recording's relative shape.

    **DC-aware:** the per-buffer mean (DC component) is subtracted
    before measuring RMS, so we operate on the actual AC signal rather
    than total power. v0.1.65 used total RMS and ended up under-
    boosting voice when a DC pedestal (TLV320 ADC with HPF disabled,
    pre-v0.1.66) made the signal look louder than it actually was.
    The DC is also removed from the saved samples — if the codec's
    HPF was off, this catches the pedestal before it reaches Whisper.

    Capped at ``max_gain_db`` so a purely-silent recording doesn't get
    amplified to clipping noise. Skips application when the needed
    boost is less than ``min_gain_db`` (already loud enough).

    Returns ``(new_frames, current_rms_db, applied_gain_db)`` — both
    diagnostics are reported back so the caller can log the decision.
    ``current_rms_db`` reflects the AC level (post-DC-removal).
    """
    if not frames:
        return frames, 0.0, 0.0
    samples = np.frombuffer(b"".join(frames), dtype=np.int16)
    if samples.size == 0:
        return frames, 0.0, 0.0
    # Center the signal — subtract DC mean. Float64 so we don't lose
    # the bias to int truncation before measuring.
    centered = samples.astype(np.float64) - float(np.mean(samples))
    rms = float(np.sqrt(np.mean(centered ** 2)))
    if rms < 1.0:  # essentially silent — nothing to normalize toward
        return frames, -120.0, 0.0
    current_db = 20.0 * float(np.log10(rms / 32768.0))
    needed_db = target_rms_db - current_db
    if needed_db < min_gain_db:
        # Already at target — still strip DC so downstream STT doesn't
        # see a biased waveform.
        clean = np.clip(centered, -32768, 32767).astype(np.int16)
        return [clean.tobytes()], current_db, 0.0
    gain_db = min(needed_db, max_gain_db)
    gain_linear = 10 ** (gain_db / 20.0)
    boosted = np.clip(centered * gain_linear, -32768, 32767).astype(np.int16)
    return [boosted.tobytes()], current_db, gain_db


def _write_wav(path: str, frames: List[bytes], bus: AudioBus) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(bus.channels)
        wf.setsampwidth(pyaudio.get_sample_size(bus.sample_format))
        wf.setframerate(bus.rate)
        wf.writeframes(b"".join(frames))


def _maybe_normalize(frames: List[bytes]) -> List[bytes]:
    """Apply audio normalization to ``frames`` if enabled in Config.

    Wraps ``_normalize_frames_to_target_rms`` with the Config-driven
    on/off + target RMS + max gain knobs. Logs the decision so per-
    recording behavior can be inspected without re-running.
    """
    if not Config.get_bool("audio_normalize_enabled", True):
        return frames
    target_db = Config.get_float("audio_normalize_target_rms_db", -18.0)
    max_gain_db = Config.get_float("audio_normalize_max_gain_db", 18.0)
    new_frames, current_db, applied_db = _normalize_frames_to_target_rms(
        frames, target_rms_db=target_db, max_gain_db=max_gain_db,
    )
    if applied_db > 0:
        logger.info(
            "Audio normalized",
            original_rms_db=round(current_db, 1),
            target_rms_db=target_db,
            applied_gain_db=round(applied_db, 1),
        )
    return new_frames


def listen(
    bus: AudioBus,
    *,
    subscriber_name: str = "listen",
    history_secs: float = 0.0,
    skip_secs: float = 0.0,
    quiet_wait_secs: Optional[float] = None,
    silence_threshold: Optional[int] = None,
    silence_duration: Optional[float] = None,
    min_record_secs: Optional[float] = None,
    max_record_secs: Optional[float] = None,
) -> RecordingResult:
    """Record a command from the bus until end-of-speech.

    Subscribes to ``bus`` with ``history_secs`` of pre-buffered audio,
    reads chunks, tracks consecutive-silence frames, and stops when the
    silence window has been exceeded AND the minimum record length has
    been reached. Writes the captured audio to ``command.wav`` and
    returns metadata.

    Two pre-recording phases ahead of the main loop:

    * **quiet-wait** (when ``quiet_wait_secs > 0``): drains chunks until
      the bus has been below ``listen_quiet_rms_threshold`` for
      ``listen_quiet_consecutive_secs``, OR ``quiet_wait_secs`` elapses.
      Replaces the v0.1.64 fixed-time ``skip_secs=0.3`` hack — instead of
      guessing how long the TTS-response tail takes to fade, we measure
      it. Defaults pulled from Config when None.
    * **skip_secs** (when ``skip_secs > 0``): legacy fixed-time drain on
      top of quiet-wait. Default 0 now that quiet-wait does the job per-
      room. Kept for callers that explicitly want a fixed offset.

    All timing knobs fall back to live Config if not overridden — so the
    state machine can pass longer silence windows for command capture
    without hard-coding them.
    """
    defaults = _audio_defaults()
    silence_threshold = silence_threshold if silence_threshold is not None else defaults["silence_threshold"]
    silence_duration = silence_duration if silence_duration is not None else defaults["silence_duration"]
    min_record_secs = min_record_secs if min_record_secs is not None else defaults["min_record_seconds"]
    max_record_secs = max_record_secs if max_record_secs is not None else defaults["max_record_seconds"]
    if quiet_wait_secs is None:
        quiet_wait_secs = Config.get_float("listen_quiet_max_wait_secs", 0.8)

    output_filename = str(_cache_dir / "command.wav")
    chunk_secs = bus.chunk_samples / bus.rate
    silence_frames_threshold = max(1, int(silence_duration / chunk_secs))
    min_frames = max(1, int(min_record_secs / chunk_secs))
    max_frames = max(min_frames, int(max_record_secs / chunk_secs))

    logger.info(
        "Listening for speech",
        history_secs=history_secs,
        quiet_wait_secs=quiet_wait_secs,
        skip_secs=skip_secs,
        silence_threshold=silence_threshold,
        silence_duration=silence_duration,
        min_seconds=min_record_secs,
        max_seconds=max_record_secs,
    )

    q = bus.subscribe(subscriber_name, history_secs=history_secs)
    frames: List[bytes] = []
    silence_frames = 0
    hit_max = False
    try:
        # Phase 1: wait for the room to actually go quiet before recording
        # starts. Kitchen-node beta test (May 2026) showed TTS reverb tail
        # of 400-700 ms causing Whisper to transcribe the speaker's own
        # response back as a "user" command. This phase measures the
        # actual mic energy and proceeds the moment it dips below the
        # quiet threshold — per-room adaptive, no tuning required.
        if quiet_wait_secs > 0:
            quiet_rms = Config.get_float(
                "listen_quiet_rms_threshold",
                float(silence_threshold) * 1.5,
            )
            quiet_consec = Config.get_float(
                "listen_quiet_consecutive_secs", 0.16,
            )
            drained, max_rms, reached_quiet = _drain_until_quiet(
                q,
                chunk_secs=chunk_secs,
                max_wait_secs=quiet_wait_secs,
                quiet_rms_threshold=quiet_rms,
                consecutive_quiet_secs=quiet_consec,
            )
            logger.info(
                "Speaker-quiet wait complete",
                drained_frames=drained,
                drained_ms=int(drained * chunk_secs * 1000),
                max_rms_during_wait=round(max_rms, 1),
                quiet_threshold=round(quiet_rms, 1),
                reached_quiet=reached_quiet,
            )

        # Phase 2: legacy fixed-time skip on top, for callers that want
        # a deterministic extra pad. Default 0 — most callers should
        # rely on the quiet-wait above instead.
        skip_chunks = max(0, int(skip_secs / chunk_secs)) if skip_secs > 0 else 0
        for _ in range(skip_chunks):
            try:
                q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                break

        for frame_count in range(max_frames):
            try:
                data = q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                logger.warning("listen() timeout waiting for audio chunk")
                break

            frames.append(data)
            rms = calculate_rms(data)

            if rms < silence_threshold:
                silence_frames += 1
            else:
                silence_frames = 0

            if silence_frames >= silence_frames_threshold and frame_count >= min_frames:
                logger.debug("Silence detected, stopping recording", silence_duration=silence_duration)
                break

            if frame_count % 50 == 0:
                elapsed = frame_count * chunk_secs
                logger.debug(
                    "Recording progress",
                    elapsed=f"{elapsed:.1f}s",
                    rms=f"{rms:.0f}",
                    silence_frames=silence_frames,
                    silence_threshold_frames=silence_frames_threshold,
                )
        else:
            hit_max = True
    finally:
        bus.unsubscribe(subscriber_name)

    actual_duration = len(frames) * chunk_secs
    logger.info("Recording complete", duration=f"{actual_duration:.2f}s", hit_max=hit_max)

    frames = _maybe_normalize(frames)
    _write_wav(output_filename, frames, bus)
    return RecordingResult(output_filename, actual_duration, hit_max)


def record_fixed_duration(
    bus: AudioBus,
    seconds: float,
    output_path: str,
    *,
    subscriber_name: str = "fixed_record",
    skip_secs: float = 0.0,
) -> RecordingResult:
    """Record exactly ``seconds`` of audio from the bus and write a WAV.

    Unlike ``listen()``, this ignores silence — useful for voice-profile
    enrollment where we want a fixed-length sample regardless of pauses.
    """
    chunk_secs = bus.chunk_samples / bus.rate
    skip_chunks = max(0, int(skip_secs / chunk_secs)) if skip_secs > 0 else 0
    record_chunks = max(1, int(seconds / chunk_secs))

    logger.info(
        "Fixed-duration recording starting",
        seconds=seconds,
        skip_secs=skip_secs,
        output=output_path,
    )

    q = bus.subscribe(subscriber_name)
    frames: List[bytes] = []
    try:
        for _ in range(skip_chunks):
            try:
                q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                break
        for _ in range(record_chunks):
            try:
                data = q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                logger.warning("record_fixed_duration: timeout waiting for chunk")
                break
            frames.append(data)
    finally:
        bus.unsubscribe(subscriber_name)

    actual_duration = len(frames) * chunk_secs
    logger.info("Fixed-duration recording complete", duration=f"{actual_duration:.2f}s")
    _write_wav(output_path, frames, bus)
    return RecordingResult(output_path, actual_duration, hit_max_duration=True)


def listen_for_follow_up(
    bus: AudioBus,
    *,
    subscriber_name: str = "follow_up",
    timeout_seconds: float = 5.0,
    history_secs: float = 0.0,
    quiet_wait_secs: Optional[float] = None,
    silence_threshold: Optional[int] = None,
    silence_duration: Optional[float] = None,
    max_record_secs: Optional[float] = None,
    min_speech_secs: Optional[float] = None,
    follow_up_iteration: int = 1,
) -> str | None:
    """Listen for follow-up speech within a timeout window.

    Subscribes to the bus, optionally drains chunks until the room goes
    quiet (replaces the v0.1.64 ``follow_up_tts_settle_secs`` sleep —
    same fix as ``listen()``), then waits up to ``timeout_seconds`` for
    speech onset (consecutive frames with RMS >= silence_threshold). If
    detected, switches to normal recording mode until silence. If the
    timeout expires without speech, returns None.

    Returns None if the recorded audio is shorter than ``min_speech_secs``
    — brief noise bursts (door closing, cough, fan gust) produce sub-second
    clips that Whisper hallucinates text from, causing infinite follow-up
    loops when the hallucinated text is sent to the command center.

    Defaults ``history_secs=0`` — follow-up cares about NEW speech, not
    the tail of the preceding TTS.
    """
    defaults = _audio_defaults()
    silence_threshold = silence_threshold if silence_threshold is not None else defaults["silence_threshold"]
    # Follow-up silence-stop is more forgiving than initial-command listen.
    # The defaults["silence_duration"] is tuned for the initial listen (where
    # min_seconds=3 floor protects against early cutoff). Follow-up has no
    # such floor — a 0.3 s pause between words was ending recording at
    # ~0.5 s, producing sub-second clips that Whisper transcribes as
    # [BLANK_AUDIO]. 0.5 s rides through most natural inter-word pauses
    # (typically 100-400 ms) while keeping the trailing wait short
    # enough that the LED flip to "thinking" feels responsive.
    if silence_duration is None:
        silence_duration = Config.get_float("follow_up_silence_duration", 0.5)
    max_record_secs = max_record_secs if max_record_secs is not None else defaults["max_record_seconds"]
    if min_speech_secs is None:
        # Lowered from 0.7 → 0.3 on 2026-05-20: 0.7 was discarding the
        # shortest valid follow-up replies ("yes" ~250 ms, "no" ~200 ms,
        # "yeah" ~350 ms, "thanks" ~500 ms) as noise. The trade-off is
        # marginally higher risk that a sub-half-second ambient burst
        # (chair scrape, door close) gets sent to Whisper, but the
        # downstream _is_non_speech filter catches the obvious
        # [BLANK_AUDIO]/[silence] hallucinations. Settable via
        # ``follow_up_min_speech_secs`` if a room turns out to be noisier.
        min_speech_secs = Config.get_float("follow_up_min_speech_secs", 0.3)
    if quiet_wait_secs is None:
        # Smaller default than the initial-listen quiet-wait (0.8) because
        # the post-TTS gap before a follow-up is typically shorter — the
        # response TTS tends to be brief ("OK, done.") so the speaker
        # tail decays faster. Tunable via ``follow_up_quiet_wait_secs``.
        quiet_wait_secs = Config.get_float("follow_up_quiet_wait_secs", 0.5)

    output_filename = str(_cache_dir / "follow_up.wav")
    chunk_secs = bus.chunk_samples / bus.rate
    silence_frames_threshold = max(1, int(silence_duration / chunk_secs))
    max_frames = max(1, int(max_record_secs / chunk_secs))
    # Require consecutive frames above threshold to confirm speech onset.
    # Lowered base 5 → 3 on 2026-05-20: 5 frames (~400 ms at 48 kHz /
    # 3840-sample chunks) was strict enough that oscillating-amplitude
    # speech (the natural case at conversational distance) kept resetting
    # the counter and onset never confirmed within the 5 s window. 3 frames
    # (~240 ms) still rejects single sharp bursts (clap, door close) since
    # those are sub-frame events, but catches real speech faster.
    base_onset = 3
    onset_required = min(15, base_onset + (follow_up_iteration - 1) * 3)

    logger.debug(
        "Follow-up listening window opened",
        timeout_seconds=timeout_seconds,
        quiet_wait_secs=quiet_wait_secs,
    )

    q = bus.subscribe(subscriber_name, history_secs=history_secs)
    frames: List[bytes] = []
    speech_detected = False
    onset_count = 0
    # Diagnostic: track max RMS seen during the wait so we can tell
    # "mic never heard the user" (max low) apart from "mic heard but
    # onset_required not met" (max high but oscillating).
    max_rms_seen = 0.0
    chunks_seen = 0
    onset_deadline = time.monotonic() + timeout_seconds
    try:
        # Quiet-wait phase — drain the speaker tail before onset detection
        # starts. Replaces the v0.1.64 follow_up_tts_settle_secs sleep with
        # an adaptive measurement: proceed the moment the room is actually
        # quiet, instead of guessing 0.6 s. The onset_deadline is NOT
        # extended; the timeout budget includes any quiet-wait time the
        # caller's deadline already accounted for.
        if quiet_wait_secs > 0:
            quiet_rms = Config.get_float(
                "listen_quiet_rms_threshold",
                float(silence_threshold) * 1.5,
            )
            quiet_consec = Config.get_float(
                "listen_quiet_consecutive_secs", 0.16,
            )
            drained, qwait_max_rms, reached_quiet = _drain_until_quiet(
                q,
                chunk_secs=chunk_secs,
                max_wait_secs=quiet_wait_secs,
                quiet_rms_threshold=quiet_rms,
                consecutive_quiet_secs=quiet_consec,
            )
            logger.info(
                "Follow-up speaker-quiet wait complete",
                drained_frames=drained,
                drained_ms=int(drained * chunk_secs * 1000),
                max_rms_during_wait=round(qwait_max_rms, 1),
                quiet_threshold=round(quiet_rms, 1),
                reached_quiet=reached_quiet,
            )
        while time.monotonic() < onset_deadline:
            remaining = max(0.05, onset_deadline - time.monotonic())
            try:
                data = q.get(timeout=min(remaining, 0.5))
            except queue.Empty:
                continue

            rms = calculate_rms(data)
            chunks_seen += 1
            if rms > max_rms_seen:
                max_rms_seen = rms
            if rms >= silence_threshold:
                onset_count += 1
                frames.append(data)
                if onset_count >= onset_required:
                    speech_detected = True
                    logger.info("Follow-up speech detected", rms=f"{rms:.0f}",
                                onset_required=onset_required)
                    break
            else:
                onset_count = 0
                frames.clear()

        if not speech_detected:
            logger.info(
                "No follow-up speech detected, timeout expired",
                timeout=round(timeout_seconds, 1),
                chunks_seen=chunks_seen,
                max_rms_seen=round(max_rms_seen, 0),
                silence_threshold=silence_threshold,
                onset_required=onset_required,
            )
            return None

        # Minimum recording window after onset: 0.7 s. Even with the longer
        # silence_duration above, a single fricative ("sh", "f") tail can
        # dip RMS below threshold for several frames in a row; the min
        # window ensures we capture at least a coherent phrase regardless.
        # 0.7 s is enough for a short reply ("yes please", "the second one")
        # while keeping the worst-case trailing wait short for short replies.
        min_record_after_onset = Config.get_float("follow_up_min_record_after_onset_secs", 0.7)
        min_frames_after_onset = max(1, int(min_record_after_onset / chunk_secs))

        silence_frames = 0
        post_onset_frames = 0
        for _ in range(max_frames):
            try:
                data = q.get(timeout=max(chunk_secs * 10, 1.0))
            except queue.Empty:
                logger.warning("listen_for_follow_up timeout waiting for chunk")
                break

            frames.append(data)
            post_onset_frames += 1
            rms = calculate_rms(data)
            if rms < silence_threshold:
                silence_frames += 1
            else:
                silence_frames = 0

            # Don't allow silence-stop to fire until we've recorded enough
            # post-onset audio for Whisper to have something to work with.
            if post_onset_frames < min_frames_after_onset:
                continue

            if silence_frames >= silence_frames_threshold:
                logger.debug("Follow-up recording: silence detected, stopping")
                break
    finally:
        bus.unsubscribe(subscriber_name)

    actual_duration = len(frames) * chunk_secs

    # Discard sub-threshold recordings. Brief noise bursts that slip past
    # onset detection produce tiny WAVs that Whisper hallucinates text from
    # ("Thank you", "You", etc.), keeping the follow-up loop alive.
    if actual_duration < min_speech_secs:
        logger.info(
            "Follow-up recording too short, discarding as noise",
            duration=f"{actual_duration:.2f}s",
            min_required=f"{min_speech_secs:.1f}s",
        )
        return None

    logger.info("Follow-up recording complete", duration=f"{actual_duration:.2f}s")
    frames = _maybe_normalize(frames)
    _write_wav(output_filename, frames, bus)
    return output_filename
