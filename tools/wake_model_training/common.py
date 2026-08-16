"""Shared constants + audio math for the music-robust wake-model pipeline.

Everything the pipeline downloads or clones is pinned HERE, in one place,
so a rerun a year from now builds the same model:

* openWakeWord training code — ``OPENWAKEWORD_REPO`` @ ``OPENWAKEWORD_REF``
* piper-sample-generator (synthetic positives) — ``PIPER_SAMPLE_GENERATOR_REPO``
  @ ``PIPER_SAMPLE_GENERATOR_REF``, TTS checkpoint from the v2.0.0 release
* Pre-computed negative features + validation features (openWakeWord's
  published .npy files on HuggingFace)
* MUSAN music/speech (OpenSLR 17, CC BY 4.0) for the loud-kitchen mixing
* MIT environmental room impulse responses for reverb character

The SNR-mixing math lives here (pure numpy + stdlib ``wave``) so it is
unit-testable on the node repo's venv without soundfile/librosa, and so
``augment_music.py`` and ``evaluate.py`` share one definition of "SNR".

Convention: all audio in this pipeline is 16 kHz mono int16 WAV — the
input format openWakeWord models score (the node captures 48 kHz and
downsamples to 16 kHz before scoring; clips produced here skip that hop).
"""

from __future__ import annotations

import wave
from pathlib import Path

import numpy as np

# ---------------------------------------------------------------------------
# Pinned sources (verified 2026-08-16)
# ---------------------------------------------------------------------------

# openWakeWord training pipeline (train.py --generate_clips / --augment_clips
# / --train_model; notebooks/automatic_model_training.ipynb is the recipe).
OPENWAKEWORD_REPO = "https://github.com/dscripka/openWakeWord"
OPENWAKEWORD_REF = "368c03716d1e92591906a84949bc477f3a834455"  # main, 2025-12-30

# Synthetic "hey jarvis" positives. The LibriTTS-R multi-speaker checkpoint
# supports 904 speakers (docs recommend --max-speakers a bit below that).
PIPER_SAMPLE_GENERATOR_REPO = "https://github.com/rhasspy/piper-sample-generator"
PIPER_SAMPLE_GENERATOR_REF = "2971426a55072f7d22fec416ca7800df8bd23207"  # master, 2026-03-12
PIPER_TTS_CHECKPOINT_URL = (
    "https://github.com/rhasspy/piper-sample-generator/releases/download/"
    "v2.0.0/en_US-libritts_r-medium.pt"
)
PIPER_MAX_SPEAKERS = 900  # < 904 to avoid the documented artifact band

# openWakeWord's published pre-computed features (HuggingFace dataset
# davidscripka/openwakeword_features):
#   - ~2,000 hrs of general negative audio features (ACAV100M)
#   - ~11 hrs validation features for false-positive-rate estimation
OWW_FEATURES_DATASET = "davidscripka/openwakeword_features"
OWW_NEGATIVE_FEATURES_URL = (
    "https://huggingface.co/datasets/davidscripka/openwakeword_features/"
    "resolve/main/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
)
OWW_VALIDATION_FEATURES_URL = (
    "https://huggingface.co/datasets/davidscripka/openwakeword_features/"
    "resolve/main/validation_set_features.npy"
)

# Background audio for openWakeWord's own augment stage (same sets the
# upstream notebook uses).
AUDIOSET_DATASET = "agkphysics/AudioSet"          # bal_train09.tar in the notebook
FMA_DATASET = "rudraml/fma"                        # "small" split

# MUSAN (OpenSLR 17) — 660 music files + speech + noise, ~109 hrs total.
# License: CC BY 4.0 (each subdirectory carries a LICENSE file with
# per-file attribution). This is the primary MUSIC source for the
# loud-kitchen SNR mixing and for music-only negatives.
MUSAN_URL = "https://www.openslr.org/resources/17/musan.tar.gz"
MUSAN_LICENSE = "CC BY 4.0 (see per-subdirectory LICENSE files in the corpus)"

# MIT environmental impulse responses — kitchen/room reverb character.
MIT_RIR_DATASET = "davidscripka/MIT_environmental_impulse_responses"
MIT_RIR_URL = (
    "https://huggingface.co/datasets/davidscripka/"
    "MIT_environmental_impulse_responses"
)

# Stock model this pipeline aims to replace (the baseline in evaluate.py).
STOCK_MODEL_NAME = "hey_jarvis"
MUSIC_MODEL_NAME = "hey_jarvis_music"

# ---------------------------------------------------------------------------
# The loud-kitchen SNR regime (June 2026 findings)
# ---------------------------------------------------------------------------
# With music playing at kitchen volume, stock hey_jarvis OWW scores cap at
# 0.14-0.43 on REAL wake phrases; the speaker-bleed-only band is 0.10-0.18.
# We therefore train/evaluate across +10 dB (background music) down to
# -10 dB (music louder than the voice — the failing regime).
DEFAULT_SNR_GRID_DB: tuple[float, ...] = (10.0, 5.0, 0.0, -5.0, -10.0)

# June 2026 recordings — tiny (~65 s over 5 files, 48 kHz stereo). Far too
# small to train on; they are the REAL-ROOM EVAL SET. augment_music.py
# refuses to route them into training output.
JUNE_RECORDINGS_DIR = Path("prds/wake-during-music/recordings")

SAMPLE_RATE = 16_000

# ---------------------------------------------------------------------------
# WAV I/O (stdlib only — the node venv has no soundfile)
# ---------------------------------------------------------------------------


def read_wav_mono_16k(path: str | Path) -> np.ndarray:
    """Read a WAV file as float32 mono in [-1, 1] at 16 kHz.

    Downmixes multi-channel by averaging. Resamples with polyphase
    filtering when the source rate differs (48 kHz node/June recordings).
    Raises ValueError for non-16-bit PCM.
    """
    with wave.open(str(path), "rb") as w:
        n_channels = w.getnchannels()
        sample_width = w.getsampwidth()
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())

    if sample_width != 2:
        raise ValueError(f"{path}: expected 16-bit PCM, got {8 * sample_width}-bit")

    samples = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if n_channels > 1:
        samples = samples.reshape(-1, n_channels).mean(axis=1)

    if rate != SAMPLE_RATE:
        from scipy.signal import resample_poly  # local: keep module import-light

        from math import gcd

        g = gcd(SAMPLE_RATE, rate)
        samples = resample_poly(samples, up=SAMPLE_RATE // g, down=rate // g)
        samples = samples.astype(np.float32)

    return samples


def write_wav_mono_16k(path: str | Path, samples: np.ndarray) -> None:
    """Write float samples in [-1, 1] as 16 kHz mono 16-bit PCM WAV."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm = (clipped * 32767.0).astype(np.int16)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# SNR math
# ---------------------------------------------------------------------------


def rms(samples: np.ndarray) -> float:
    """Root-mean-square level of a float signal."""
    if samples.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples.astype(np.float64)))))


def measure_snr_db(signal: np.ndarray, noise: np.ndarray) -> float:
    """SNR in dB between a clean signal and the noise that was mixed in."""
    s, n = rms(signal), rms(noise)
    if n == 0.0:
        return float("inf")
    if s == 0.0:
        return float("-inf")
    return 20.0 * np.log10(s / n)


def fit_noise_length(noise: np.ndarray, n_samples: int, offset: int = 0) -> np.ndarray:
    """Tile-or-trim ``noise`` to exactly ``n_samples``, starting at ``offset``.

    Music tracks are minutes long and positives are ~2 s; ``offset`` lets the
    caller pull a different window per clip so mixes don't all share one bar
    of music.
    """
    if noise.size == 0:
        raise ValueError("noise is empty")
    offset = offset % noise.size
    if noise.size - offset >= n_samples:
        return noise[offset:offset + n_samples]
    reps = int(np.ceil((n_samples + offset) / noise.size))
    return np.tile(noise, reps)[offset:offset + n_samples]


def mix_at_snr(
    signal: np.ndarray,
    noise: np.ndarray,
    snr_db: float,
    peak: float = 0.99,
) -> tuple[np.ndarray, float]:
    """Mix ``noise`` into ``signal`` at the requested SNR.

    The noise is scaled so that ``20*log10(rms(signal)/rms(scaled_noise))``
    equals ``snr_db``; the signal is left untouched (so wake-phrase energy
    matches the source clip), then the SUM is rescaled only if it would
    clip beyond ``peak``.

    Returns ``(mix, applied_gain)`` where ``applied_gain`` is the final
    post-sum scale factor (1.0 when no clipping rescue was needed).
    Raises ValueError when signal or noise is silent (SNR undefined).
    """
    if signal.ndim != 1 or noise.ndim != 1:
        raise ValueError("mix_at_snr expects mono 1-D arrays")
    if noise.size != signal.size:
        raise ValueError(
            f"length mismatch: signal={signal.size} noise={noise.size} "
            "(use fit_noise_length first)"
        )
    s_rms, n_rms = rms(signal), rms(noise)
    if s_rms == 0.0:
        raise ValueError("signal is silent; SNR undefined")
    if n_rms == 0.0:
        raise ValueError("noise is silent; SNR undefined")

    target_noise_rms = s_rms / (10.0 ** (snr_db / 20.0))
    scaled_noise = noise * (target_noise_rms / n_rms)
    mix = signal + scaled_noise

    applied_gain = 1.0
    peak_abs = float(np.max(np.abs(mix))) if mix.size else 0.0
    if peak_abs > peak:
        applied_gain = peak / peak_abs
        mix = mix * applied_gain

    return mix.astype(np.float32), applied_gain


def apply_rir(signal: np.ndarray, rir: np.ndarray) -> np.ndarray:
    """Convolve a signal with a room impulse response, energy-normalized.

    Output is trimmed to the input length and rescaled to preserve the dry
    signal's RMS, so an RIR pass doesn't silently change the SNR that a
    later ``mix_at_snr`` computes.
    """
    from scipy.signal import fftconvolve  # local: keep module import-light

    if rir.size == 0:
        raise ValueError("rir is empty")
    wet = fftconvolve(signal, rir)[: signal.size]
    dry_rms, wet_rms = rms(signal), rms(wet)
    if wet_rms > 0.0 and dry_rms > 0.0:
        wet = wet * (dry_rms / wet_rms)
    return wet.astype(np.float32)
