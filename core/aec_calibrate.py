"""Startup delay calibration for the inline AEC.

Plays a short linear chirp through ``paplay``, captures the mic via the
running ``AudioBus`` and pulls the matching reference window from the
``ReferenceReader``, then cross-correlates to find the actual
speaker→mic delay in samples at the AEC's working rate.

The intent is to replace the fixed ``aec_reference_delay_ms`` config
guess with a per-node measurement, so Speex's adaptive filter starts
near the right tap location instead of having to search.

Best-effort: any failure (paplay missing, PA refusing playback, mic
samples not arriving, peak SNR too low) returns ``None`` and the
caller keeps the configured default. No exception escapes.
"""

from __future__ import annotations

import queue
import shutil
import subprocess
import time
from typing import TYPE_CHECKING

import numpy as np

from jarvis_log_client import JarvisLogger

if TYPE_CHECKING:
    from core.aec_reference import ReferenceReader
    from core.audio_bus import AudioBus

logger = JarvisLogger(service="jarvis-node")


def _make_chirp(rate: int, duration_ms: int, amplitude: float = 0.35) -> np.ndarray:
    """Linear frequency sweep 800 Hz → 4500 Hz at the given rate.

    The bandwidth is wide enough to give cross-correlation a clear peak,
    while staying above 500 Hz keeps the chirp from sounding like a
    low-frequency bump and gets it past the codec's analog HPF.
    """
    n = int(rate * duration_ms / 1000)
    t = np.arange(n, dtype=np.float64) / rate
    f0, f1 = 800.0, 4500.0
    secs = duration_ms / 1000.0
    phase = 2 * np.pi * (f0 * t + 0.5 * (f1 - f0) / secs * t * t)
    return (np.sin(phase) * amplitude * 32767).astype(np.int16)


def calibrate_speaker_mic_delay(
    bus: "AudioBus",
    reference_reader: "ReferenceReader",
    *,
    aec_rate: int = 16000,
    chirp_duration_ms: int = 200,
    capture_duration_ms: int = 600,
    min_delay_ms: float = 5.0,
    max_delay_ms: float = 300.0,
    min_peak_snr: float = 3.0,
) -> int | None:
    """Play a chirp, cross-correlate mic vs reference, return delay in AEC samples.

    Returns ``None`` if calibration fails for any reason — callers should
    fall back to a configured default delay.
    """
    if not shutil.which("paplay"):
        logger.warning("AEC calibration: paplay not on PATH; skipping")
        return None

    chirp = _make_chirp(rate=aec_rate, duration_ms=chirp_duration_ms)

    sub_name = "aec_calibrate"
    try:
        q = bus.subscribe(sub_name, maxsize=256)
    except ValueError:
        logger.warning("AEC calibration: bus subscriber already registered; skipping")
        return None

    try:
        proc = subprocess.Popen(
            [
                "paplay",
                "--rate", str(aec_rate),
                "--channels", "1",
                "--format", "s16le",
                "--raw",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, FileNotFoundError) as exc:
        bus.unsubscribe(sub_name)
        logger.warning("AEC calibration: failed to spawn paplay", error=str(exc))
        return None

    # Drain any pre-subscribe chunks that may have snuck into the queue.
    # We want the capture window to be flush with the chirp.
    while True:
        try:
            q.get_nowait()
        except queue.Empty:
            break

    chirp_started = time.monotonic()
    try:
        assert proc.stdin is not None
        proc.stdin.write(chirp.tobytes())
        proc.stdin.close()
    except (BrokenPipeError, OSError) as exc:
        proc.kill()
        bus.unsubscribe(sub_name)
        logger.warning("AEC calibration: paplay write failed", error=str(exc))
        return None

    # Collect mic samples at the bus rate (typically 48 kHz) for the capture
    # window. The chirp itself is ~chirp_duration_ms; we collect longer so
    # the echo + reverb tail lands inside the window.
    target_mic_bytes = int(bus.rate * 2 * capture_duration_ms / 1000)
    mic_bytes = bytearray()
    deadline = chirp_started + (capture_duration_ms / 1000.0) + 0.5
    while len(mic_bytes) < target_mic_bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        try:
            chunk = q.get(timeout=min(remaining, 0.2))
        except queue.Empty:
            break
        mic_bytes.extend(chunk)

    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()

    bus.unsubscribe(sub_name)

    if len(mic_bytes) < target_mic_bytes // 2:
        logger.warning(
            "AEC calibration: too few mic samples collected",
            collected_bytes=len(mic_bytes),
            target_bytes=target_mic_bytes,
        )
        return None

    mic_at_bus_rate = np.frombuffer(bytes(mic_bytes), dtype=np.int16)

    # Downsample mic to the AEC rate so it aligns with reference samples.
    if bus.rate != aec_rate:
        if bus.rate % aec_rate != 0:
            logger.warning(
                "AEC calibration: bus rate not a multiple of AEC rate",
                bus_rate=bus.rate,
                aec_rate=aec_rate,
            )
            return None
        try:
            from scipy.signal import resample_poly

            mic = resample_poly(mic_at_bus_rate, up=1, down=bus.rate // aec_rate)
            mic = np.clip(mic, -32768, 32767).astype(np.int16)
        except Exception as exc:
            logger.warning("AEC calibration: resample failed", error=str(exc))
            return None
    else:
        mic = mic_at_bus_rate

    # Pull the reference window — most recent buffer-worth of samples.
    # The chirp just played so the reference has it. Cap to the mic length
    # so the cross-correlation lags map cleanly.
    n_ref = min(len(mic), reference_reader.buffer_capacity_samples)
    ref = reference_reader.pull(n_samples=n_ref, delay_samples=0, max_stale_secs=2.0)
    if ref is None:
        logger.warning("AEC calibration: reference reader returned None (no fresh samples)")
        return None

    return _correlate_for_delay(
        mic=mic,
        ref=ref,
        aec_rate=aec_rate,
        min_delay_ms=min_delay_ms,
        max_delay_ms=max_delay_ms,
        min_peak_snr=min_peak_snr,
    )


def _correlate_for_delay(
    *,
    mic: np.ndarray,
    ref: np.ndarray,
    aec_rate: int,
    min_delay_ms: float,
    max_delay_ms: float,
    min_peak_snr: float,
) -> int | None:
    """Cross-correlate mic against ref; return positive lag (mic samples after ref) or None."""
    try:
        from scipy.signal import correlate
    except ImportError as exc:
        logger.warning("AEC calibration: scipy unavailable", error=str(exc))
        return None

    n = min(len(mic), len(ref))
    if n < aec_rate * 0.1:  # need at least 100 ms
        logger.warning("AEC calibration: too few samples for correlation", n=n)
        return None
    mic_n = mic[:n]
    ref_n = ref[:n]

    mic_norm = max(float(np.abs(mic_n).max()), 1.0)
    ref_norm = max(float(np.abs(ref_n).max()), 1.0)
    mic_f = mic_n.astype(np.float64) / mic_norm
    ref_f = ref_n.astype(np.float64) / ref_norm

    # correlate(a, b)[i] is sum of a[n] * b[n - (i - len(b) + 1)] — the
    # peak is at index len(b) - 1 + lag where lag > 0 means a (mic) lags b (ref).
    corr = correlate(mic_f, ref_f, mode="full")
    lags = np.arange(-len(ref_f) + 1, len(mic_f))

    min_lag = int(min_delay_ms * aec_rate / 1000)
    max_lag = int(max_delay_ms * aec_rate / 1000)
    mask = (lags >= min_lag) & (lags <= max_lag)
    if not np.any(mask):
        logger.warning("AEC calibration: lag search range empty")
        return None

    valid_lags = lags[mask]
    valid_corr = corr[mask]
    peak_idx = int(np.argmax(np.abs(valid_corr)))
    best_lag = int(valid_lags[peak_idx])
    peak_abs = float(np.abs(valid_corr[peak_idx]))

    noise_floor = float(np.median(np.abs(corr)))
    snr = peak_abs / max(noise_floor, 1e-9)
    if snr < min_peak_snr:
        logger.warning(
            "AEC calibration: peak SNR too low; keeping configured default",
            snr=round(snr, 2),
            best_lag=best_lag,
            best_lag_ms=round(best_lag * 1000.0 / aec_rate, 1),
        )
        return None

    logger.info(
        "AEC delay calibrated",
        delay_samples=best_lag,
        delay_ms=round(best_lag * 1000.0 / aec_rate, 1),
        snr=round(snr, 2),
    )
    return best_lag
