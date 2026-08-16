#!/usr/bin/env python3
"""Held-out evaluation: hey_jarvis_music vs stock hey_jarvis.

This report is the deployment decision. The music model REPLACES stock on
the node (one model per node — 60-80 ms of an 80 ms budget; dual-model
scoring does not fit), so it must beat stock where music plays AND hold
the line everywhere else.

Metrics:

  * Recall per SNR bucket on music-mixed positives (the eval_holdout
    slice that augment_music.py reserved — never seen in training).
  * False accepts per hour on music-only and speech-only negatives.
  * The same numbers for STOCK hey_jarvis on the same sets — the baseline
    that justifies (or blocks) deployment.
  * Threshold sweep (0.3-0.9) so the wake threshold setting can be
    retuned for the new model rather than inherited.
  * Optionally: scores on the June 2026 real-kitchen recordings and on
    the real auto-labeled wake-clip corpus (below).

Real wake-clip corpus format (--real-clips-dir):

    Since node v0.2.4 every wake fire writes a consumed-chunks clip
    (~2 s, 16 kHz mono int16 WAV — the exact audio the scorer consumed)
    and CC's wake verification issues a verdict. Export those as:

        <dir>/clips/<clip_id>.wav
        <dir>/labels.jsonl      # one JSON object per line:
            {"file": "clips/abc123.wav",   # relative path
             "verdict": "confirmed",       # confirmed | false_wake
             "oww_score": 0.61,            # node-side score at fire time
             "self_playback": false,       # music-duck telemetry flag
             "node_id": "...", "ts": "..."}   # optional provenance

    verdict=confirmed clips count as positives, false_wake as negatives.
    This corpus is score-biased (only fires above the node threshold get
    clips — no true-negative audio), so it yields per-clip score deltas
    vs stock, not absolute recall/FA rates.

Usage:

    python evaluate.py \\
        --model artifacts/hey_jarvis_music.onnx \\
        --eval-dir ./data/augmented/eval_holdout \\
        --compare-stock \\
        --june-recordings ../../prds/wake-during-music/recordings \\
        --real-clips-dir ./data/real_clips \\
        --report artifacts/eval_report.json
    python evaluate.py --dry-run --model artifacts/hey_jarvis_music.onnx

Needs: pip install openwakeword (pulls onnxruntime). Runs on a laptop —
no GPU required.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    SAMPLE_RATE,
    STOCK_MODEL_NAME,
    read_wav_mono_16k,
)

# The node scores 80 ms chunks (1280 samples @ 16 kHz) — mirror that.
FRAME_SAMPLES = 1280
DEFAULT_THRESHOLD = 0.5
THRESHOLD_SWEEP = [0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--model", required=True,
                   help="Candidate .onnx (or .tflite) wake model")
    p.add_argument("--eval-dir", default=None,
                   help="eval_holdout dir from augment_music.py "
                        "(positives_music/snr_*/, negatives_*/)")
    p.add_argument("--compare-stock", action="store_true",
                   help=f"Also score stock '{STOCK_MODEL_NAME}' on every set")
    p.add_argument("--june-recordings", default=None,
                   help="prds/wake-during-music/recordings (real kitchen; "
                        "48 kHz stereo handled automatically)")
    p.add_argument("--real-clips-dir", default=None,
                   help="Auto-labeled wake-clip corpus (see module docstring)")
    p.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    p.add_argument("--report", default=None,
                   help="Write full JSON report here")
    p.add_argument("--dry-run", action="store_true",
                   help="Validate inputs and print the plan; score nothing")
    return p.parse_args(argv)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def load_model(model_path_or_name: str):
    """Load an openWakeWord model (custom path or stock pretrained name)."""
    from openwakeword.model import Model  # lazy: heavy import

    if Path(model_path_or_name).suffix in (".onnx", ".tflite"):
        inference = ("onnx" if model_path_or_name.endswith(".onnx")
                     else "tflite")
        return Model(wakeword_models=[model_path_or_name],
                     inference_framework=inference)
    # Stock pretrained name (e.g. "hey_jarvis") — openwakeword downloads/
    # locates its bundled model.
    return Model(wakeword_models=[model_path_or_name])


def score_clip(model, samples: np.ndarray) -> float:
    """Max frame score over a clip, streamed in node-sized 80 ms frames."""
    model.reset()
    pcm = (np.clip(samples, -1.0, 1.0) * 32767.0).astype(np.int16)
    max_score = 0.0
    for start in range(0, len(pcm) - FRAME_SAMPLES + 1, FRAME_SAMPLES):
        prediction = model.predict(pcm[start:start + FRAME_SAMPLES])
        max_score = max(max_score, max(prediction.values()))
    return float(max_score)


def score_directory(model, wav_dir: Path) -> list[dict]:
    results = []
    for wav in sorted(wav_dir.rglob("*.wav")):
        samples = read_wav_mono_16k(wav)
        results.append({
            "file": str(wav.relative_to(wav_dir)),
            "seconds": round(len(samples) / SAMPLE_RATE, 2),
            "score": round(score_clip(model, samples), 4),
        })
    return results


def recall_by_bucket(scored: list[dict], threshold: float) -> dict[str, dict]:
    """Group positives by their snr_* path component and compute recall."""
    buckets: dict[str, list[float]] = {}
    for r in scored:
        parts = Path(r["file"]).parts
        bucket = next((p for p in parts if p.startswith("snr_")), "unbucketed")
        buckets.setdefault(bucket, []).append(r["score"])
    out = {}
    for bucket, scores in sorted(buckets.items()):
        hits = sum(1 for s in scores if s >= threshold)
        out[bucket] = {
            "n": len(scores),
            "recall": round(hits / len(scores), 4) if scores else None,
            "median_score": round(float(np.median(scores)), 4),
        }
    return out


def false_accepts_per_hour(scored: list[dict], threshold: float) -> dict:
    total_seconds = sum(r["seconds"] for r in scored)
    accepts = sum(1 for r in scored if r["score"] >= threshold)
    hours = total_seconds / 3600.0
    return {
        "n_clips": len(scored),
        "audio_hours": round(hours, 3),
        "false_accepts": accepts,
        "fa_per_hour": round(accepts / hours, 3) if hours > 0 else None,
    }


def sweep(scored_pos: list[dict], scored_neg: list[dict]) -> list[dict]:
    rows = []
    for t in THRESHOLD_SWEEP:
        pos_hits = sum(1 for r in scored_pos if r["score"] >= t)
        rows.append({
            "threshold": t,
            "recall": round(pos_hits / len(scored_pos), 4) if scored_pos else None,
            **({"fa_per_hour": false_accepts_per_hour(scored_neg, t)["fa_per_hour"]}
               if scored_neg else {}),
        })
    return rows


def eval_real_clips(model, clips_dir: Path, threshold: float) -> dict:
    """Score the auto-labeled corpus; report score deltas by CC verdict."""
    labels_path = clips_dir / "labels.jsonl"
    if not labels_path.is_file():
        return {"error": f"{labels_path} not found (see module docstring)"}
    rows = []
    with open(labels_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            label = json.loads(line)
            wav = clips_dir / label["file"]
            if not wav.is_file():
                continue
            samples = read_wav_mono_16k(wav)
            rows.append({
                **label,
                "candidate_score": round(score_clip(model, samples), 4),
            })
    by_verdict: dict[str, list[dict]] = {}
    for r in rows:
        by_verdict.setdefault(r.get("verdict", "unknown"), []).append(r)
    summary = {}
    for verdict, group in by_verdict.items():
        scores = [g["candidate_score"] for g in group]
        summary[verdict] = {
            "n": len(group),
            "median_candidate_score": round(float(np.median(scores)), 4),
            "over_threshold": sum(1 for s in scores if s >= threshold),
        }
    return {"summary_by_verdict": summary, "clips": rows}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def evaluate_model(name: str, model_ref: str, args: argparse.Namespace) -> dict:
    print(f"\n--- scoring: {name} ({model_ref}) ---")
    model = load_model(model_ref)
    report: dict = {"model": model_ref}

    if args.eval_dir:
        eval_dir = Path(args.eval_dir)
        pos_dir = eval_dir / "positives_music"
        scored_pos = score_directory(model, pos_dir) if pos_dir.is_dir() else []
        scored_neg = []
        for neg_name in ("negatives_music_only", "negatives_music_speech"):
            neg_dir = eval_dir / neg_name
            if neg_dir.is_dir():
                scored = score_directory(model, neg_dir)
                report[f"fa_{neg_name}"] = false_accepts_per_hour(
                    scored, args.threshold)
                scored_neg += scored
        if scored_pos:
            report["recall_by_snr"] = recall_by_bucket(scored_pos, args.threshold)
            report["threshold_sweep"] = sweep(scored_pos, scored_neg)

    if args.june_recordings:
        june_dir = Path(args.june_recordings)
        if june_dir.is_dir():
            report["june_recordings"] = score_directory(model, june_dir)

    if args.real_clips_dir:
        report["real_clips"] = eval_real_clips(
            model, Path(args.real_clips_dir), args.threshold)

    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    sets = []
    if args.eval_dir:
        sets.append(f"eval_holdout: {args.eval_dir}")
    if args.june_recordings:
        sets.append(f"june real-kitchen: {args.june_recordings}")
    if args.real_clips_dir:
        sets.append(f"real wake clips: {args.real_clips_dir}")

    print("=" * 60)
    print("WAKE MODEL EVALUATION")
    print("=" * 60)
    print(f"  candidate:  {args.model}")
    print(f"  baseline:   {STOCK_MODEL_NAME if args.compare_stock else '(skipped)'}")
    print(f"  threshold:  {args.threshold}")
    for s in sets:
        print(f"  set:        {s}")
    if not sets:
        print("  ⚠️ no eval sets given — pass --eval-dir / --june-recordings /"
              " --real-clips-dir")

    if args.dry_run:
        missing = [d for d in [args.eval_dir, args.june_recordings,
                               args.real_clips_dir]
                   if d and not Path(d).is_dir()]
        if not Path(args.model).is_file() and Path(args.model).suffix:
            missing.append(args.model)
        if missing:
            print(f"\nDRY RUN — missing inputs: {missing}")
            return 1
        print("\nDRY RUN — inputs look valid; nothing scored.")
        return 0

    if not sets:
        return 1

    full_report = {
        "threshold": args.threshold,
        "candidate": evaluate_model("candidate", args.model, args),
    }
    if args.compare_stock:
        full_report["stock_baseline"] = evaluate_model(
            "stock", STOCK_MODEL_NAME, args)

    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    for which in ("candidate", "stock_baseline"):
        if which not in full_report:
            continue
        rep = full_report[which]
        print(f"\n{which}: {rep['model']}")
        for bucket, stats in rep.get("recall_by_snr", {}).items():
            print(f"  {bucket:10s} recall={stats['recall']} "
                  f"(n={stats['n']}, median={stats['median_score']})")
        for key in ("fa_negatives_music_only", "fa_negatives_music_speech"):
            if key in rep:
                print(f"  {key}: {rep[key]['fa_per_hour']} FA/hr "
                      f"over {rep[key]['audio_hours']} hrs")

    if args.report:
        Path(args.report).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report).write_text(json.dumps(full_report, indent=2))
        print(f"\n📄 full report → {args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
