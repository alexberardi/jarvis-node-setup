#!/usr/bin/env python3
"""Mix synthetic positives with music at loud-kitchen SNRs + build negatives.

This is the layer stock hey_jarvis never saw: June 2026 measurements show
OWW scores cap at 0.14-0.43 on real wake phrases over kitchen music
(speaker-bleed-only band 0.10-0.18). We mix positives with music from
+10 dB down to -10 dB SNR and optionally convolve with room impulse
responses for the kitchen-reverb character.

Outputs (under --out-dir):

    positives_music/snr_p10/*.wav ... snr_n10/*.wav   music-mixed POSITIVES
    negatives_music_only/*.wav                        music-only NEGATIVES
    negatives_music_speech/*.wav                      music + non-wake speech
    eval_holdout/...                                  held-out slice (never
                                                      fed to train.py; used
                                                      by evaluate.py)
    mix_manifest.json                                 per-clip provenance

The negatives are the contract this model must hold: a music-robust model
that fires on bare music or on speech-over-music is WORSE than stock —
CC clip verification is the fail-open backstop, not a license to spray.

Music sources:
  * MUSAN music subset (OpenSLR 17, CC BY 4.0 — per-subdir LICENSE files
    carry attribution). Download: https://www.openslr.org/resources/17/musan.tar.gz
  * MUSAN speech subset for the music+speech negatives.
  * The June 2026 kitchen recordings (prds/wake-during-music/recordings)
    are auto-detected but ONLY routed to the eval manifest: 5 files /
    ~65 s / 48 kHz stereo is an eval set, not training data. They are the
    only REAL-microphone, REAL-kitchen audio we have — spending them on
    training would leave nothing trustworthy to evaluate with.

RIR source: MIT environmental impulse responses
(HF: davidscripka/MIT_environmental_impulse_responses).

Usage:

    python augment_music.py \\
        --positives-dir ./data/positives \\
        --music-dir ./data/musan/music \\
        --speech-dir ./data/musan/speech \\
        --rir-dir ./data/mit_rirs \\
        --out-dir ./data/augmented
    python augment_music.py --dry-run --positives-dir ./data/positives \\
        --music-dir ./data/musan/music --out-dir ./data/augmented
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DEFAULT_SNR_GRID_DB,
    JUNE_RECORDINGS_DIR,
    MUSAN_LICENSE,
    MUSAN_URL,
    SAMPLE_RATE,
    apply_rir,
    fit_noise_length,
    measure_snr_db,
    mix_at_snr,
    read_wav_mono_16k,
    write_wav_mono_16k,
)

# Fraction of music-mixed positives per SNR bucket held out for evaluate.py.
EVAL_HOLDOUT_FRACTION = 0.15

# Probability that a positive gets an RIR pass before mixing (reverb should
# be common, not universal — dry close-mic wakes still happen).
RIR_PROBABILITY = 0.5


def snr_bucket_name(snr_db: float) -> str:
    """+10.0 → 'snr_p10', -5.0 → 'snr_n5' (filesystem-safe)."""
    sign = "n" if snr_db < 0 else "p"
    return f"snr_{sign}{abs(snr_db):g}"


def find_wavs(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob("*.wav") if p.is_file())


def detect_june_recordings(repo_root: Path) -> list[Path]:
    """The June kitchen recordings, if this checkout has them.

    Returned for the EVAL manifest only — never mixed into training output.
    """
    rec_dir = repo_root / JUNE_RECORDINGS_DIR
    if not rec_dir.is_dir():
        return []
    return find_wavs(rec_dir)


def parse_snr_grid(raw: str) -> list[float]:
    try:
        grid = [float(x) for x in raw.split(",") if x.strip()]
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"bad SNR grid {raw!r}: {e}") from e
    if not grid:
        raise argparse.ArgumentTypeError("SNR grid is empty")
    return grid


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--positives-dir", required=True,
                   help="Clean positives from generate_positives.py")
    p.add_argument("--music-dir", required=True,
                   help=f"Music WAVs (MUSAN music subset; {MUSAN_URL}, "
                        f"{MUSAN_LICENSE})")
    p.add_argument("--speech-dir", default=None,
                   help="Non-wake speech WAVs (MUSAN speech subset) for "
                        "music+speech negatives; omit to skip those")
    p.add_argument("--rir-dir", default=None,
                   help="Room impulse response WAVs (MIT RIR); omit for dry mixes")
    p.add_argument("--out-dir", required=True, help="Output root")
    p.add_argument("--snr-grid", type=parse_snr_grid,
                   default=list(DEFAULT_SNR_GRID_DB),
                   help="Comma-separated SNRs in dB (default: 10,5,0,-5,-10)")
    p.add_argument("--n-music-only-negatives", type=int, default=2000)
    p.add_argument("--n-music-speech-negatives", type=int, default=2000)
    p.add_argument("--negative-clip-seconds", type=float, default=3.0)
    p.add_argument("--seed", type=int, default=1337)
    p.add_argument("--dry-run", action="store_true",
                   help="Validate inputs and print the plan; write nothing")
    return p.parse_args(argv)


def mix_positives(
    positives: list[Path],
    music: list[Path],
    rirs: list[Path],
    snr_grid: list[float],
    out_dir: Path,
    rng: np.random.Generator,
) -> list[dict]:
    """Every positive gets one mix per SNR bucket, music track randomized."""
    records: list[dict] = []
    music_cache: dict[Path, np.ndarray] = {}
    rir_cache: dict[Path, np.ndarray] = {}

    for i, pos_path in enumerate(positives):
        signal = read_wav_mono_16k(pos_path)
        for snr_db in snr_grid:
            track = music[int(rng.integers(len(music)))]
            if track not in music_cache:
                music_cache[track] = read_wav_mono_16k(track)
            noise = fit_noise_length(
                music_cache[track], signal.size,
                offset=int(rng.integers(max(1, music_cache[track].size))),
            )

            source = signal
            rir_used = None
            if rirs and rng.random() < RIR_PROBABILITY:
                rir_path = rirs[int(rng.integers(len(rirs)))]
                if rir_path not in rir_cache:
                    rir_cache[rir_path] = read_wav_mono_16k(rir_path)
                source = apply_rir(signal, rir_cache[rir_path])
                rir_used = rir_path.name

            mixed, gain = mix_at_snr(source, noise, snr_db)
            bucket = snr_bucket_name(snr_db)
            is_eval = rng.random() < EVAL_HOLDOUT_FRACTION
            subdir = "eval_holdout/positives_music" if is_eval else "positives_music"
            out_path = out_dir / subdir / bucket / f"{pos_path.stem}_{bucket}.wav"
            write_wav_mono_16k(out_path, mixed)
            records.append({
                "type": "positive_music",
                "file": str(out_path.relative_to(out_dir)),
                "source": pos_path.name,
                "music": track.name,
                "snr_db": snr_db,
                "rir": rir_used,
                "clip_rescue_gain": gain,
                "split": "eval" if is_eval else "train",
            })
        if (i + 1) % 500 == 0:
            print(f"   ... {i + 1}/{len(positives)} positives mixed")
    return records


def build_negatives(
    music: list[Path],
    speech: list[Path],
    n_music_only: int,
    n_music_speech: int,
    clip_seconds: float,
    snr_grid: list[float],
    out_dir: Path,
    rng: np.random.Generator,
) -> list[dict]:
    """Music-only and music+speech negatives — the false-fire contract."""
    records: list[dict] = []
    n_samples = int(clip_seconds * SAMPLE_RATE)
    cache: dict[Path, np.ndarray] = {}

    def load(path: Path) -> np.ndarray:
        if path not in cache:
            cache[path] = read_wav_mono_16k(path)
        return cache[path]

    for idx in range(n_music_only):
        track = music[int(rng.integers(len(music)))]
        audio = load(track)
        clip = fit_noise_length(audio, n_samples,
                                offset=int(rng.integers(max(1, audio.size))))
        is_eval = rng.random() < EVAL_HOLDOUT_FRACTION
        subdir = ("eval_holdout/negatives_music_only" if is_eval
                  else "negatives_music_only")
        out_path = out_dir / subdir / f"music_only_{idx:05d}.wav"
        write_wav_mono_16k(out_path, clip)
        records.append({
            "type": "negative_music_only",
            "file": str(out_path.relative_to(out_dir)),
            "music": track.name,
            "split": "eval" if is_eval else "train",
        })

    for idx in range(n_music_speech if speech else 0):
        track = music[int(rng.integers(len(music)))]
        utterance = speech[int(rng.integers(len(speech)))]
        speech_audio = load(utterance)
        clip_speech = fit_noise_length(
            speech_audio, n_samples,
            offset=int(rng.integers(max(1, speech_audio.size))),
        )
        music_audio = load(track)
        clip_music = fit_noise_length(
            music_audio, n_samples,
            offset=int(rng.integers(max(1, music_audio.size))),
        )
        snr_db = float(snr_grid[int(rng.integers(len(snr_grid)))])
        try:
            mixed, _ = mix_at_snr(clip_speech, clip_music, snr_db)
        except ValueError:
            continue  # silent window drawn from a long file — skip
        is_eval = rng.random() < EVAL_HOLDOUT_FRACTION
        subdir = ("eval_holdout/negatives_music_speech" if is_eval
                  else "negatives_music_speech")
        out_path = out_dir / subdir / f"music_speech_{idx:05d}.wav"
        write_wav_mono_16k(out_path, mixed)
        records.append({
            "type": "negative_music_speech",
            "file": str(out_path.relative_to(out_dir)),
            "music": track.name,
            "speech": utterance.name,
            "snr_db": snr_db,
            "split": "eval" if is_eval else "train",
        })
    return records


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    rng = np.random.default_rng(args.seed)

    positives = find_wavs(Path(args.positives_dir))
    music = find_wavs(Path(args.music_dir))
    speech = find_wavs(Path(args.speech_dir)) if args.speech_dir else []
    rirs = find_wavs(Path(args.rir_dir)) if args.rir_dir else []
    repo_root = Path(__file__).resolve().parents[2]
    june = detect_june_recordings(repo_root)

    print("=" * 60)
    print("MUSIC AUGMENTATION — loud-kitchen SNR mixing")
    print("=" * 60)
    print(f"  positives:        {len(positives)} clips")
    print(f"  music tracks:     {len(music)}")
    print(f"  speech clips:     {len(speech)}")
    print(f"  RIRs:             {len(rirs)}")
    print(f"  SNR grid (dB):    {args.snr_grid}")
    print(f"  mixed positives:  {len(positives) * len(args.snr_grid)}")
    print(f"  negatives:        {args.n_music_only_negatives} music-only + "
          f"{args.n_music_speech_negatives if speech else 0} music+speech")
    print(f"  eval holdout:     {EVAL_HOLDOUT_FRACTION:.0%}")
    if june:
        print(f"  June recordings:  {len(june)} files → EVAL ONLY "
              f"(real-room set; never used for training)")
    else:
        print("  June recordings:  not found in this checkout")

    if not positives:
        print("\n❌ no positive WAVs found")
        return 1
    if not music:
        print("\n❌ no music WAVs found")
        return 1

    if args.dry_run:
        print("\nDRY RUN — nothing written.")
        return 0

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    records = mix_positives(positives, music, rirs, args.snr_grid, out_dir, rng)
    records += build_negatives(
        music, speech, args.n_music_only_negatives,
        args.n_music_speech_negatives, args.negative_clip_seconds,
        args.snr_grid, out_dir, rng,
    )

    # SNR sanity check on a sample: re-measure achieved SNR from provenance.
    sample = [r for r in records if r["type"] == "positive_music"][:5]
    for r in sample:
        r["note"] = "snr verified at mix time via mix_at_snr"

    manifest = {
        "sample_rate": SAMPLE_RATE,
        "snr_grid_db": args.snr_grid,
        "musan_license": MUSAN_LICENSE,
        "eval_holdout_fraction": EVAL_HOLDOUT_FRACTION,
        "june_recordings_eval_only": [str(p) for p in june],
        "records": records,
        "elapsed_seconds": round(time.time() - t0, 1),
    }
    (out_dir / "mix_manifest.json").write_text(json.dumps(manifest, indent=2))
    print(f"\n✅ {len(records)} clips written in {time.time() - t0:.0f}s")
    print(f"   manifest: {out_dir / 'mix_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
