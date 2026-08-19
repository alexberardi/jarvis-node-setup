#!/usr/bin/env python3
"""Synthesize "hey jarvis" positive clips via piper-sample-generator.

Produces N (default 5000) 16 kHz mono WAV clips spanning many voices,
speaking rates, and prosody, plus a smaller set of ADVERSARIAL near-miss
phrases ("hey jarvie", "hey travis", "jarvis" alone, ...) that train.py
uses as hard negatives.

Two TTS options exist for this fleet:

1. **piper-sample-generator (default, what this script drives).** The
   LibriTTS-R medium checkpoint gives 904 speakers — the voice DIVERSITY
   a wake model needs. Repo/checkpoint pinned in common.py.
2. **jarvis-tts (the fleet's own Piper voice).** The node hears its own
   TTS voice constantly (self-playback), so a few hundred positives AND
   negatives in the production voice are worth adding later via
   ``jarvis-tts /synthesize``. NOT implemented here — the fleet voice is
   a single speaker and must never dominate the training set; note kept
   so nobody re-derives it.

Setup (one-time, ~1 GB):

    git clone https://github.com/rhasspy/piper-sample-generator
    git -C piper-sample-generator checkout <PIPER_SAMPLE_GENERATOR_REF>
    wget <PIPER_TTS_CHECKPOINT_URL> -P piper-sample-generator/models/

Usage:

    python generate_positives.py --psg-dir ./piper-sample-generator \\
        --out-dir ./data/positives --n 5000
    python generate_positives.py --dry-run --n 5000

Runs locally (CPU is fine, GPU faster) or on the RunPod pod — the remote
harness invokes this same script there.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    PIPER_MAX_SPEAKERS,
    PIPER_SAMPLE_GENERATOR_REF,
    PIPER_SAMPLE_GENERATOR_REPO,
    PIPER_TTS_CHECKPOINT_URL,
)

TARGET_PHRASE = "hey jarvis"

# Spelling variants nudging the phonemizer toward pronunciations we hear
# in the wild. All are POSITIVES.
POSITIVE_TEXTS = [
    "hey jarvis",
    "hey, jarvis",
    "hey jarvis.",
    "hay jarvis",       # phonemizer nudge, same phones
    "heyy jarvis",      # drawn-out onset
]

# Near-miss phrases used as hard negatives by openWakeWord's train.py
# (adversarial texts). ~10% of --n is generated for these.
ADVERSARIAL_TEXTS = [
    "hey jarvie",
    "hey travis",
    "hey harvest",
    "jarvis",
    "hey are those",
    "hey java script",
    "they are just",
]

# Prosody grid: piper-sample-generator maps length-scale ≈ 1/speed and
# noise-scales control variability. Ranges follow the upstream
# automatic_model_training.ipynb defaults, widened slightly on speed —
# people call over music FAST and LOUD.
LENGTH_SCALES = [0.7, 0.85, 1.0, 1.15, 1.3]
NOISE_SCALES = [0.333, 0.667]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--n", type=int, default=5000,
                   help="Total positive clips to synthesize (default 5000)")
    p.add_argument("--n-adversarial", type=int, default=None,
                   help="Adversarial near-miss clips (default: n // 10)")
    p.add_argument("--psg-dir", default="./piper-sample-generator",
                   help="Path to a piper-sample-generator checkout")
    p.add_argument("--model", default=None,
                   help="Piper checkpoint .pt (default: "
                        "<psg-dir>/models/en_US-libritts_r-medium.pt)")
    p.add_argument("--out-dir", default="./data/positives",
                   help="Output dir; adversarial clips go to <out-dir>_adversarial")
    p.add_argument("--batch-size", type=int, default=50)
    p.add_argument("--max-speakers", type=int, default=PIPER_MAX_SPEAKERS,
                   help="Speaker cap (<904 avoids LibriTTS-R artifact voices)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the generation plan without synthesizing")
    return p.parse_args()


def build_jobs(n: int, n_adversarial: int) -> list[dict]:
    """Split the clip budget across texts and prosody settings."""
    jobs: list[dict] = []

    def spread(texts: list[str], total: int, kind: str) -> None:
        per_text = max(1, total // len(texts))
        for text in texts:
            jobs.append({
                "kind": kind,
                "text": text,
                "max_samples": per_text,
                "length_scales": LENGTH_SCALES,
                "noise_scales": NOISE_SCALES,
            })

    spread(POSITIVE_TEXTS, n, "positive")
    spread(ADVERSARIAL_TEXTS, n_adversarial, "adversarial")
    return jobs


def psg_command(job: dict, args: argparse.Namespace, out_dir: Path) -> list[str]:
    """Build the generate_samples.py invocation for one job."""
    model = args.model or str(
        Path(args.psg_dir) / "models" / "en_US-libritts_r-medium.pt"
    )
    cmd = [
        sys.executable,
        str(Path(args.psg_dir) / "generate_samples.py"),
        job["text"],
        "--model", model,
        "--max-samples", str(job["max_samples"]),
        "--batch-size", str(args.batch_size),
        "--max-speakers", str(args.max_speakers),
        "--output-dir", str(out_dir),
    ]
    for ls in job["length_scales"]:
        cmd += ["--length-scales", str(ls)]
    for ns in job["noise_scales"]:
        cmd += ["--noise-scales", str(ns)]
    return cmd



def _finalize_job_wavs(tmp_dir: Path, dest: Path, prefix: str) -> int:
    """Convert one job's WAVs to 16 kHz mono PCM16 under collision-proof names.

    piper-tts writes WAVE_FORMAT_EXTENSIBLE float WAVs that stdlib `wave`
    (used downstream in augment_music/common) cannot read, and every
    generate_samples.py invocation names files 0.wav..N.wav — so multiple
    jobs sharing one output dir silently overwrite each other (this run
    produced 1000 of 5000 requested positives before the fix). Each job now
    generates into a private tmp dir and is flattened here with a unique
    prefix. Returns the number of files finalized.
    """
    import shutil
    import numpy as np
    from scipy.io import wavfile
    from scipy.signal import resample_poly

    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for src in sorted(tmp_dir.glob("*.wav")):
        rate, data = wavfile.read(str(src))
        if data.ndim > 1:
            data = data.mean(axis=1)
        if data.dtype != np.float32 and data.dtype != np.float64:
            data = data.astype(np.float32) / max(abs(np.iinfo(data.dtype).min), 1)
        if rate != 16000:
            from math import gcd
            g = gcd(rate, 16000)
            data = resample_poly(data, 16000 // g, rate // g)
        pcm = np.clip(data, -1.0, 1.0)
        wavfile.write(str(dest / f"{prefix}_{src.stem}.wav"), 16000,
                      (pcm * 32767.0).astype(np.int16))
        count += 1
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return count


def main() -> int:
    args = parse_args()
    n_adv = args.n_adversarial if args.n_adversarial is not None else args.n // 10
    jobs = build_jobs(args.n, n_adv)
    out_pos = Path(args.out_dir)
    out_adv = Path(str(args.out_dir).rstrip("/") + "_adversarial")

    print("=" * 60)
    print("SYNTHETIC POSITIVES — piper-sample-generator")
    print("=" * 60)
    print(f"  repo:        {PIPER_SAMPLE_GENERATOR_REPO}")
    print(f"  pinned ref:  {PIPER_SAMPLE_GENERATOR_REF}")
    print(f"  checkpoint:  {PIPER_TTS_CHECKPOINT_URL}")
    print(f"  positives:   {args.n} → {out_pos}")
    print(f"  adversarial: {n_adv} → {out_adv}")
    print(f"  jobs:        {len(jobs)}")

    if args.dry_run:
        print("\nDRY RUN — commands that would run:")
        for job in jobs:
            dest = out_pos if job["kind"] == "positive" else out_adv
            print("  $ " + " ".join(psg_command(job, args, dest)))
        return 0

    psg = Path(args.psg_dir)
    if not (psg / "generate_samples.py").is_file():
        print(f"\n❌ {psg}/generate_samples.py not found.")
        print("   Clone + pin it first (see module docstring).")
        return 1

    manifest: list[dict] = []
    t0 = time.time()
    for i, job in enumerate(jobs, 1):
        dest = out_pos if job["kind"] == "positive" else out_adv
        tmp = dest.parent / f".tmp_{job['kind']}_{i:02d}"
        tmp.mkdir(parents=True, exist_ok=True)
        cmd = psg_command(job, args, tmp)
        print(f"\n[{i}/{len(jobs)}] {job['kind']}: \"{job['text']}\" "
              f"x{job['max_samples']}")
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"   ❌ failed: {result.stderr[-500:]}")
            return 1
        n_done = _finalize_job_wavs(tmp, dest, f"{job['kind']}{i:02d}")
        if n_done == 0:
            print(f"   ❌ job produced no WAVs — treating as failure")
            return 1
        print(f"   → {n_done} clips finalized (16k mono PCM16)")
        manifest.append({**job, "output_dir": str(dest), "n_finalized": n_done})

    (out_pos / "generation_manifest.json").write_text(json.dumps({
        "target_phrase": TARGET_PHRASE,
        "psg_ref": PIPER_SAMPLE_GENERATOR_REF,
        "checkpoint": PIPER_TTS_CHECKPOINT_URL,
        "jobs": manifest,
        "elapsed_seconds": round(time.time() - t0, 1),
    }, indent=2))
    print(f"\n✅ Done in {time.time() - t0:.0f}s. "
          f"Manifest: {out_pos / 'generation_manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
