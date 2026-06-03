"""Offline analysis of the 2026-06-03 diagnostic recordings.

Runs five questions through the stereo + reference WAVs and writes the
numerical answers to stdout. The PRD lives at
``prds/wake-during-music.md``; this script feeds into
``prds/wake-during-music/analysis.md``.

Usage::

    cd jarvis-node-setup
    .venv/bin/python prds/wake-during-music/analyze.py

The recordings directory (``prds/wake-during-music/recordings/``) must
contain ``voice_only.wav``, ``music_only_mic.wav``,
``music_only_ref.wav``, ``voice_music_mic.wav``,
``voice_music_ref.wav``. WAVs are stereo s16le 48 kHz.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
from scipy.io import wavfile
from scipy.signal import (
    coherence,
    correlate,
    correlation_lags,
    welch,
)

REC_DIR = Path(__file__).parent / "recordings"
RATE_EXPECTED = 48000


def load_stereo(name: str) -> tuple[int, np.ndarray]:
    """Return (rate, samples) where samples is (n, 2) float64 normalized to [-1, 1)."""
    rate, data = wavfile.read(REC_DIR / name)
    if data.dtype == np.int16:
        data = data.astype(np.float64) / 32768.0
    if data.ndim == 1:
        data = data[:, np.newaxis]
    if data.shape[1] != 2:
        raise ValueError(f"{name}: expected stereo, got {data.shape}")
    return rate, data


def rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(x.astype(np.float64) ** 2)))


def clipping_fraction(x: np.ndarray, threshold: float = 0.99) -> float:
    return float(np.mean(np.abs(x) >= threshold))


def mono_avg(stereo: np.ndarray) -> np.ndarray:
    return 0.5 * (stereo[:, 0] + stereo[:, 1])


# ---------------------------------------------------------------------------
# Question 1: True acoustic delay (mic vs reference)
# ---------------------------------------------------------------------------

def measure_delay(mic: np.ndarray, ref: np.ndarray, rate: int) -> dict:
    """Cross-correlate to find lag where mic = ref delayed by L samples."""
    # Use mono averages of mic and ref so we get one number, not per-channel.
    m = mono_avg(mic)
    r = mono_avg(ref)
    # Trim to common length, remove DC, taper edges to suppress wraparound peak.
    n = min(len(m), len(r))
    m = m[:n] - m[:n].mean()
    r = r[:n] - r[:n].mean()
    # Limit search range to physically plausible delay (-50 ms to +500 ms).
    max_lag = int(rate * 0.5)  # 500 ms
    min_lag = int(rate * -0.05)  # -50 ms (allow tiny negative for clock skew)
    corr = correlate(m, r, mode="full")
    lags = correlation_lags(len(m), len(r), mode="full")
    mask = (lags >= min_lag) & (lags <= max_lag)
    valid_lags = lags[mask]
    valid_corr = corr[mask]
    # Peak. Use absolute value because room can invert phase.
    peak_idx = int(np.argmax(np.abs(valid_corr)))
    best_lag = int(valid_lags[peak_idx])
    peak_val = float(valid_corr[peak_idx])
    noise_floor = float(np.median(np.abs(valid_corr)))
    snr = abs(peak_val) / max(noise_floor, 1e-12)
    return {
        "lag_samples": best_lag,
        "lag_ms": round(best_lag * 1000.0 / rate, 2),
        "peak_corr": round(peak_val, 4),
        "snr_vs_median": round(snr, 1),
        "search_range_ms": (
            round(min_lag * 1000.0 / rate, 1),
            round(max_lag * 1000.0 / rate, 1),
        ),
    }


# ---------------------------------------------------------------------------
# Question 2: Mic L vs Mic R coherence (does stereo help?)
# ---------------------------------------------------------------------------

def stereo_coherence(mic: np.ndarray, rate: int) -> dict:
    """γ²(f) between L and R mic channels. High = stereo gives no new info."""
    # Welch coherence with 1024-point segments → ~47 Hz resolution at 48 kHz.
    nperseg = 1024
    f, c = coherence(mic[:, 0], mic[:, 1], fs=rate, nperseg=nperseg)
    # Band stats — wake-relevant range is 80-4000 Hz (OWW input).
    band_mask = (f >= 80) & (f <= 4000)
    coh_band = c[band_mask]
    # Same for sub-bands so we can see if coherence drops at high freq
    # (which is where ~30 mm baseline mics would start to differ).
    def bin_stat(lo: float, hi: float) -> float:
        m = (f >= lo) & (f < hi)
        return float(np.median(c[m])) if np.any(m) else float("nan")
    return {
        "median_80_4000Hz": round(float(np.median(coh_band)), 3),
        "p10_80_4000Hz": round(float(np.percentile(coh_band, 10)), 3),
        "p90_80_4000Hz": round(float(np.percentile(coh_band, 90)), 3),
        "median_80_500Hz": round(bin_stat(80, 500), 3),
        "median_500_2000Hz": round(bin_stat(500, 2000), 3),
        "median_2000_4000Hz": round(bin_stat(2000, 4000), 3),
        "median_4000_8000Hz": round(bin_stat(4000, 8000), 3),
    }


# ---------------------------------------------------------------------------
# Question 3: Voice vs music spectral overlap
# ---------------------------------------------------------------------------

def psd_comparison(voice: np.ndarray, music: np.ndarray, rate: int) -> dict:
    """Welch PSDs of voice (no music) and music (no voice). Returns peak
    bands and a "voice-dominant" fraction (how much of the wake-band has
    voice PSD > music PSD by 3 dB)."""
    nperseg = 4096  # ~12 Hz resolution
    f_v, p_v = welch(mono_avg(voice), fs=rate, nperseg=nperseg)
    f_m, p_m = welch(mono_avg(music), fs=rate, nperseg=nperseg)
    # Normalize each to peak so the comparison is shape-based, not gain-based.
    p_v_norm = p_v / max(p_v.max(), 1e-30)
    p_m_norm = p_m / max(p_m.max(), 1e-30)
    band = (f_v >= 80) & (f_v <= 4000)
    db_v = 10 * np.log10(p_v_norm[band] + 1e-30)
    db_m = 10 * np.log10(p_m_norm[band] + 1e-30)
    voice_dominant = float(np.mean((db_v - db_m) > 3.0))
    # Peak frequency in voice
    voice_peak_f = float(f_v[int(np.argmax(p_v_norm * (f_v >= 80) * (f_v <= 4000)))])
    music_peak_f = float(f_m[int(np.argmax(p_m_norm * (f_m >= 80) * (f_m <= 4000)))])
    return {
        "voice_peak_hz": round(voice_peak_f, 0),
        "music_peak_hz": round(music_peak_f, 0),
        "voice_dominant_fraction_80_4000Hz": round(voice_dominant, 3),
    }


# ---------------------------------------------------------------------------
# Question 4: Reverb tail / minimum AEC filter length
# ---------------------------------------------------------------------------

def reverb_estimate(mic: np.ndarray, ref: np.ndarray, rate: int) -> dict:
    """Estimate the impulse response from ref → mic and measure how long
    it stays significant. Sets the minimum AEC filter length needed.

    Method: deconvolution via cross-spectrum, then find where the energy
    in the impulse response drops by 30 dB from its peak (RT30-style).
    """
    # Mono signals.
    m = mono_avg(mic)
    r = mono_avg(ref)
    n = min(len(m), len(r))
    m = m[:n]
    r = r[:n]
    # Cross-correlation is a quick proxy for the impulse response when
    # the source is broadband — it's the IR convolved with the source
    # autocorrelation, but for music the autocorrelation decorrelates
    # quickly so the peak shape ~ IR shape.
    corr = correlate(m, r, mode="full")
    lags = correlation_lags(len(m), len(r), mode="full")
    # Take only physically plausible delays (0 to 1 s)
    pos = (lags >= 0) & (lags <= int(rate * 1.0))
    ir = corr[pos]
    lags_pos = lags[pos]
    # Find peak (the direct-path delay)
    peak_idx = int(np.argmax(np.abs(ir)))
    peak = float(np.abs(ir[peak_idx]))
    if peak <= 0:
        return {"rt_estimate_ms": None}
    # Envelope = abs of windowed IR, then find -30 dB drop point after peak.
    env = np.abs(ir)
    env_db = 20 * np.log10(env / peak + 1e-12)
    # Walk forward from peak until we hit -30 dB AND stay below it for 10 ms.
    threshold_db = -30.0
    window_samples = int(rate * 0.01)  # 10 ms
    decay_idx = None
    for i in range(peak_idx + 1, len(env_db) - window_samples):
        if np.all(env_db[i : i + window_samples] < threshold_db):
            decay_idx = i
            break
    if decay_idx is None:
        return {"rt_estimate_ms": None, "note": "no -30 dB decay found in 1 s"}
    rt_samples = decay_idx - peak_idx
    return {
        "peak_lag_samples": int(lags_pos[peak_idx]),
        "peak_lag_ms": round(lags_pos[peak_idx] * 1000.0 / rate, 2),
        "rt_to_minus_30db_samples": int(rt_samples),
        "rt_to_minus_30db_ms": round(rt_samples * 1000.0 / rate, 1),
    }


# ---------------------------------------------------------------------------
# Question 5: Coherence(mic, ref) — theoretical AEC ceiling
# ---------------------------------------------------------------------------

def mic_ref_coherence(
    mic: np.ndarray, ref: np.ndarray, rate: int, delay_samples: int = 0,
) -> dict:
    """γ²(mic, ref) after applying the measured speaker→mic delay.

    Without alignment the Welch windows see mismatched chunks and γ² collapses
    even when the underlying signals are perfectly linearly related. We pull
    the reference back by ``delay_samples`` so segment k of mic lines up with
    segment k of the (delayed) reference.
    """
    m = mono_avg(mic)
    r = mono_avg(ref)
    if delay_samples > 0:
        m = m[delay_samples:]
        r = r[: len(m)]
    elif delay_samples < 0:
        r = r[-delay_samples:]
        m = m[: len(r)]
    n = min(len(m), len(r))
    nperseg = 4096
    f, c = coherence(m[:n], r[:n], fs=rate, nperseg=nperseg)
    band = (f >= 80) & (f <= 4000)
    coh_band = c[band]
    median_coh = float(np.median(coh_band))
    # γ²-based optimal Wiener-filter suppression bound:
    # residual_power_fraction = 1 - γ² → max suppression in dB.
    return {
        "delay_applied_ms": round(delay_samples * 1000.0 / rate, 2),
        "median_coh_80_4000Hz": round(median_coh, 3),
        "p10_coh_80_4000Hz": round(float(np.percentile(coh_band, 10)), 3),
        "p90_coh_80_4000Hz": round(float(np.percentile(coh_band, 90)), 3),
        "implied_max_suppression_dB": round(
            -10 * np.log10(max(1 - median_coh, 1e-6)), 1
        ),
    }


# ---------------------------------------------------------------------------
# Recording sanity check
# ---------------------------------------------------------------------------

def sanity(name: str, x: np.ndarray) -> dict:
    return {
        "rms_L": round(rms(x[:, 0]), 4),
        "rms_R": round(rms(x[:, 1]), 4),
        "peak_L": round(float(np.abs(x[:, 0]).max()), 4),
        "peak_R": round(float(np.abs(x[:, 1]).max()), 4),
        "clip_frac_L": round(clipping_fraction(x[:, 0]), 5),
        "clip_frac_R": round(clipping_fraction(x[:, 1]), 5),
        "duration_s": round(len(x) / RATE_EXPECTED, 2),
    }


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def pretty(d: dict) -> None:
    width = max(len(k) for k in d)
    for k, v in d.items():
        print(f"  {k.ljust(width)}  {v}")


def main() -> int:
    print(f"Recordings dir: {REC_DIR}")
    if not REC_DIR.exists():
        print("Missing recordings dir", file=sys.stderr)
        return 1

    # Load
    rate_vo, voice_only = load_stereo("voice_only.wav")
    rate_mm, music_mic = load_stereo("music_only_mic.wav")
    rate_mr, music_ref = load_stereo("music_only_ref.wav")
    rate_vm, vm_mic = load_stereo("voice_music_mic.wav")
    rate_vr, vm_ref = load_stereo("voice_music_ref.wav")

    if not all(r == RATE_EXPECTED for r in (rate_vo, rate_mm, rate_mr, rate_vm, rate_vr)):
        print("Rate mismatch; expected 48000 across the board", file=sys.stderr)
        return 1

    section("Sanity / per-recording stats")
    for name, x in [
        ("voice_only", voice_only),
        ("music_only_mic", music_mic),
        ("music_only_ref", music_ref),
        ("voice_music_mic", vm_mic),
        ("voice_music_ref", vm_ref),
    ]:
        print(f"\n  {name}")
        pretty(sanity(name, x))

    section("Q1: True acoustic delay (mic vs ref, music-only)")
    delay_info = measure_delay(music_mic, music_ref, RATE_EXPECTED)
    pretty(delay_info)
    measured_delay_samples = int(delay_info["lag_samples"])

    section("Q2: Mic L vs Mic R coherence (does stereo help?)")
    print("\n  music_only_mic")
    pretty(stereo_coherence(music_mic, RATE_EXPECTED))
    print("\n  voice_music_mic (caveat: clipped)")
    pretty(stereo_coherence(vm_mic, RATE_EXPECTED))

    section("Q3: Voice vs music spectral overlap")
    pretty(psd_comparison(voice_only, music_mic, RATE_EXPECTED))

    section("Q4: Reverb / impulse response decay (ref → mic)")
    pretty(reverb_estimate(music_mic, music_ref, RATE_EXPECTED))

    section("Q5: Coherence(mic, ref) — theoretical AEC ceiling")
    print("\n  Without delay correction (sanity check, expect ~0):")
    pretty(mic_ref_coherence(music_mic, music_ref, RATE_EXPECTED, delay_samples=0))
    print(f"\n  With measured delay applied ({measured_delay_samples} samples / "
          f"{round(measured_delay_samples * 1000.0 / RATE_EXPECTED, 1)} ms):")
    pretty(mic_ref_coherence(
        music_mic, music_ref, RATE_EXPECTED, delay_samples=measured_delay_samples,
    ))
    # Also sweep a few delays around the peak in case the cross-correlation
    # found a phase-inverted match rather than the true acoustic delay.
    print("\n  Delay sweep around the measured peak:")
    for d_ms in (-30, -10, 0, 10, 30, 64, 128, 200):
        d_s = int(d_ms * RATE_EXPECTED / 1000)
        r = mic_ref_coherence(music_mic, music_ref, RATE_EXPECTED, delay_samples=d_s)
        print(f"    delay={d_ms:>4} ms  →  median γ² = {r['median_coh_80_4000Hz']:.3f}"
              f"  (max suppression {r['implied_max_suppression_dB']} dB)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
