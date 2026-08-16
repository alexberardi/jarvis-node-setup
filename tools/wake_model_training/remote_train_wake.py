#!/usr/bin/env python3
"""Train the music-robust hey_jarvis openWakeWord model. Runs ON the pod.

Follows the openWakeWord automatic_model_training.ipynb recipe with one
extra stage: the music-mixed clips from augment_music.py are merged into
the clip directories before feature extraction, so the classifier sees
the loud-kitchen regime (music at +10 → -10 dB SNR) that stock hey_jarvis
was never trained on.

Stage plan (each skippable / resumable via --stages):

  1. setup      — clone PINNED openWakeWord + piper-sample-generator,
                  download the Piper checkpoint, pre-computed negative
                  feature .npy files, MUSAN, MIT RIRs
  2. positives  — generate_positives.py (default 5000 clean clips +
                  adversarial near-miss clips)
  3. augment    — augment_music.py (SNR mixing, music-only and
                  music+speech negatives, eval holdout)
  4. merge      — copy clean + music-mixed TRAIN-split clips into
                  openWakeWord's expected layout:
                    <output_dir>/<model_name>/positive_train|positive_test
                    <output_dir>/<model_name>/negative_train|negative_test
                  (we generate clips ourselves, so train.py's
                  --generate_clips step is skipped entirely)
  5. features   — train.py --augment_clips (openWakeWord's own noise/RIR/
                  gain augmentation ON TOP of our music mixes + feature
                  extraction to memory-mapped .npy)
  6. train      — train.py --train_model → <output_dir>/<model_name>.onnx
                  and .tflite
  7. package    — copy hey_jarvis_music.onnx/.tflite + metadata.json to
                  --output for download by train_runpod.py

Config keys below are the REAL keys read by train.py at the pinned ref
(verified 2026-08-16): piper_sample_generator_path, output_dir,
model_name, rir_paths, background_paths, background_paths_duplication_rate,
target_phrase, n_samples, tts_batch_size, n_samples_val,
custom_negative_phrases, augmentation_rounds, total_length,
augmentation_batch_size, feature_data_files, batch_n_per_class,
false_positive_validation_data_path, model_type, layer_size, steps,
max_negative_weight, target_false_positives_per_hour, target_accuracy,
target_recall.

Usage (on the pod):

    python remote_train_wake.py --workspace /workspace --output /workspace/out
    python remote_train_wake.py --dry-run                  # plan only
    python remote_train_wake.py --stages setup,positives   # partial run
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (  # noqa: E402
    DEFAULT_SNR_GRID_DB,
    MUSAN_URL,
    MUSIC_MODEL_NAME,
    OPENWAKEWORD_REF,
    OPENWAKEWORD_REPO,
    OWW_NEGATIVE_FEATURES_URL,
    OWW_VALIDATION_FEATURES_URL,
    PIPER_SAMPLE_GENERATOR_REF,
    PIPER_SAMPLE_GENERATOR_REPO,
    PIPER_TTS_CHECKPOINT_URL,
)

ALL_STAGES = ["setup", "positives", "augment", "merge", "features", "train",
              "package"]

# Fraction of merged clips routed to the *_test dirs (train.py's own
# held-out split for its accuracy/recall targets — distinct from the
# eval_holdout that augment_music.py reserves for evaluate.py).
TEST_SPLIT = 0.1


def sh(cmd: str, timeout: int = 3600, check: bool = True) -> int:
    print(f"$ {cmd}")
    result = subprocess.run(cmd, shell=True, timeout=timeout)
    if check and result.returncode != 0:
        raise RuntimeError(f"command failed ({result.returncode}): {cmd}")
    return result.returncode


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--workspace", default="/workspace")
    p.add_argument("--output", default="/workspace/out")
    p.add_argument("--n-positives", type=int, default=5000)
    p.add_argument("--steps", type=int, default=50000)
    p.add_argument("--snr-grid",
                   default=",".join(str(s) for s in DEFAULT_SNR_GRID_DB))
    p.add_argument("--stages", default=",".join(ALL_STAGES),
                   help=f"Comma-separated subset of: {ALL_STAGES}")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the stage plan; run nothing")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_setup(ws: Path) -> None:
    """Clone pinned repos + download datasets. Idempotent."""
    if not (ws / "openwakeword").is_dir():
        sh(f"git clone {OPENWAKEWORD_REPO} {ws}/openwakeword")
        sh(f"git -C {ws}/openwakeword checkout {OPENWAKEWORD_REF}")
    if not (ws / "piper-sample-generator").is_dir():
        sh(f"git clone {PIPER_SAMPLE_GENERATOR_REPO} {ws}/piper-sample-generator")
        sh(f"git -C {ws}/piper-sample-generator checkout "
           f"{PIPER_SAMPLE_GENERATOR_REF}")

    models_dir = ws / "piper-sample-generator" / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    ckpt = models_dir / "en_US-libritts_r-medium.pt"
    if not ckpt.is_file():
        sh(f"wget -q {PIPER_TTS_CHECKPOINT_URL} -O {ckpt}")

    # Pre-computed features — the big time-saver: ~2,000 hrs of general
    # negatives + ~11 hrs validation, already run through the
    # melspec/embedding models.
    for url in (OWW_NEGATIVE_FEATURES_URL, OWW_VALIDATION_FEATURES_URL):
        fname = ws / url.rsplit("/", 1)[-1]
        if not fname.is_file():
            sh(f"wget -q '{url}' -O {fname}", timeout=7200)

    # MUSAN — music for SNR mixing + speech for music+speech negatives.
    if not (ws / "musan").is_dir():
        sh(f"wget -q {MUSAN_URL} -O {ws}/musan.tar.gz", timeout=7200)
        sh(f"tar -xzf {ws}/musan.tar.gz -C {ws} && rm {ws}/musan.tar.gz")

    # MIT room impulse responses (kitchen-reverb character).
    if not (ws / "mit_rirs").is_dir():
        sh(
            "python -c \""
            "from huggingface_hub import snapshot_download; "
            "snapshot_download("
            "'davidscripka/MIT_environmental_impulse_responses', "
            f"repo_type='dataset', local_dir='{ws}/mit_rirs')\"",
            timeout=3600,
        )
    sh(f"pip install -e {ws}/openwakeword")
    psg_reqs = ws / "piper-sample-generator" / "requirements.txt"
    if psg_reqs.is_file():
        sh(f"pip install -r {psg_reqs}")


def stage_positives(ws: Path, tools: Path, n: int) -> None:
    sh(
        f"python {tools}/generate_positives.py"
        f" --psg-dir {ws}/piper-sample-generator"
        f" --out-dir {ws}/data/positives"
        f" --n {n}"
    )


def stage_augment(ws: Path, tools: Path, snr_grid: str) -> None:
    sh(
        f"python {tools}/augment_music.py"
        f" --positives-dir {ws}/data/positives"
        f" --music-dir {ws}/musan/music"
        f" --speech-dir {ws}/musan/speech"
        f" --rir-dir {ws}/mit_rirs"
        f" --out-dir {ws}/data/augmented"
        f" --snr-grid '{snr_grid}'"
    )


def _copy_split(sources: list[Path], train_dir: Path, test_dir: Path) -> tuple[int, int]:
    """Deterministically split WAVs from sources into train/test dirs."""
    train_dir.mkdir(parents=True, exist_ok=True)
    test_dir.mkdir(parents=True, exist_ok=True)
    n_train = n_test = 0
    for src_dir in sources:
        if not src_dir.is_dir():
            print(f"   (skip missing {src_dir})")
            continue
        for i, wav in enumerate(sorted(src_dir.rglob("*.wav"))):
            # Deterministic modulo split — reproducible without RNG state.
            dest = test_dir if i % int(1 / TEST_SPLIT) == 0 else train_dir
            target = dest / f"{src_dir.name}_{wav.stem}.wav"
            shutil.copy2(wav, target)
            if dest is test_dir:
                n_test += 1
            else:
                n_train += 1
    return n_train, n_test


def stage_merge(ws: Path) -> None:
    """Merge our clips into openWakeWord's expected directory layout.

    train.py --augment_clips reads clips from
    <output_dir>/<model_name>/{positive,negative}_{train,test}/ — the dirs
    --generate_clips would have populated. We populate them ourselves
    (clean positives + music-mixed positives; adversarial + music
    negatives) and skip --generate_clips.

    The eval_holdout subtree from augment_music.py is deliberately NOT
    merged — that slice belongs to evaluate.py.
    """
    model_dir = ws / "oww_output" / MUSIC_MODEL_NAME
    aug = ws / "data" / "augmented"

    pos_sources = [
        ws / "data" / "positives",
        *sorted((aug / "positives_music").glob("snr_*")),
    ]
    neg_sources = [
        ws / "data" / "positives_adversarial",
        aug / "negatives_music_only",
        aug / "negatives_music_speech",
    ]
    p_train, p_test = _copy_split(
        pos_sources, model_dir / "positive_train", model_dir / "positive_test")
    n_train, n_test = _copy_split(
        neg_sources, model_dir / "negative_train", model_dir / "negative_test")
    print(f"merged clips → {model_dir}")
    print(f"  positives: {p_train} train / {p_test} test")
    print(f"  negatives: {n_train} train / {n_test} test")


def write_training_yaml(ws: Path, args: argparse.Namespace) -> Path:
    import yaml

    config = {
        # Identity
        "target_phrase": ["hey jarvis"],
        "model_name": MUSIC_MODEL_NAME,
        "output_dir": str(ws / "oww_output"),
        # Clip generation (unused — merge stage supplies clips — but
        # train.py reads these keys, so keep them valid):
        "piper_sample_generator_path": str(ws / "piper-sample-generator"),
        "n_samples": args.n_positives,
        "n_samples_val": 1000,
        "tts_batch_size": 50,
        "custom_negative_phrases": [],
        # Augmentation (train.py's own pass on top of our music mixes):
        "background_paths": [
            str(ws / "musan" / "music"),
            str(ws / "musan" / "noise"),
        ],
        "background_paths_duplication_rate": [1, 1],
        "rir_paths": [str(ws / "mit_rirs")],
        "augmentation_rounds": 1,
        "augmentation_batch_size": 16,
        "total_length": 32000,  # 2 s @ 16 kHz — matches the node's clip len
        # Training
        "feature_data_files": {
            "ACAV100M_sample": str(
                ws / "openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
            ),
        },
        "false_positive_validation_data_path": str(
            ws / "validation_set_features.npy"
        ),
        "batch_n_per_class": {
            "ACAV100M_sample": 1024,
            "adversarial_negative": 50,
            "positive": 50,
        },
        "model_type": "dnn",
        "layer_size": 32,
        "steps": args.steps,
        "max_negative_weight": 1500,
        "target_accuracy": 0.7,
        "target_recall": 0.5,
        "target_false_positives_per_hour": 0.2,
    }
    path = ws / f"{MUSIC_MODEL_NAME}.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False))
    print(f"training config → {path}")
    return path


def stage_features(ws: Path, yaml_path: Path) -> None:
    train_py = ws / "openwakeword" / "openwakeword" / "train.py"
    sh(f"python {train_py} --training_config {yaml_path} --augment_clips",
       timeout=14400)


def stage_train(ws: Path, yaml_path: Path) -> None:
    train_py = ws / "openwakeword" / "openwakeword" / "train.py"
    sh(f"python {train_py} --training_config {yaml_path} --train_model",
       timeout=14400)


def stage_package(ws: Path, out: Path, args: argparse.Namespace,
                  t0: float) -> None:
    out.mkdir(parents=True, exist_ok=True)
    produced = ws / "oww_output"
    for ext in ("onnx", "tflite"):
        src = produced / f"{MUSIC_MODEL_NAME}.{ext}"
        if not src.is_file():
            raise FileNotFoundError(f"expected model artifact missing: {src}")
        shutil.copy2(src, out / src.name)

    metadata = {
        "model_name": MUSIC_MODEL_NAME,
        "target_phrase": "hey jarvis",
        "replaces": "hey_jarvis (stock)",
        "openwakeword_repo": OPENWAKEWORD_REPO,
        "openwakeword_ref": OPENWAKEWORD_REF,
        "piper_sample_generator_ref": PIPER_SAMPLE_GENERATOR_REF,
        "piper_checkpoint": PIPER_TTS_CHECKPOINT_URL,
        "n_positives": args.n_positives,
        "snr_grid_db": args.snr_grid,
        "steps": args.steps,
        "train_duration_seconds": round(time.time() - t0, 1),
        "eval": "run tools/wake_model_training/evaluate.py locally against "
                "the downloaded artifacts + eval_holdout + June recordings",
    }
    (out / "metadata.json").write_text(json.dumps(metadata, indent=2))
    print(f"✅ artifacts in {out}: {MUSIC_MODEL_NAME}.onnx, "
          f"{MUSIC_MODEL_NAME}.tflite, metadata.json")


def main() -> int:
    args = parse_args()
    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    unknown = [s for s in stages if s not in ALL_STAGES]
    if unknown:
        print(f"❌ unknown stages: {unknown} (valid: {ALL_STAGES})")
        return 1

    ws = Path(args.workspace)
    tools = Path(__file__).resolve().parent
    out = Path(args.output)

    print("=" * 60)
    print(f"REMOTE TRAIN: {MUSIC_MODEL_NAME}")
    print("=" * 60)
    print(f"  workspace: {ws}")
    print(f"  output:    {out}")
    print(f"  stages:    {stages}")
    print(f"  positives: {args.n_positives}  steps: {args.steps}")
    print(f"  SNR grid:  {args.snr_grid}")

    if args.dry_run:
        print("\nDRY RUN — stage plan only, nothing executed.")
        return 0

    t0 = time.time()
    yaml_path = ws / f"{MUSIC_MODEL_NAME}.yaml"
    if "setup" in stages:
        stage_setup(ws)
    if "positives" in stages:
        stage_positives(ws, tools, args.n_positives)
    if "augment" in stages:
        stage_augment(ws, tools, args.snr_grid)
    if "merge" in stages:
        stage_merge(ws)
    if "features" in stages or "train" in stages:
        yaml_path = write_training_yaml(ws, args)
    if "features" in stages:
        stage_features(ws, yaml_path)
    if "train" in stages:
        stage_train(ws, yaml_path)
    if "package" in stages:
        stage_package(ws, out, args, t0)

    print(f"\nDone in {(time.time() - t0) / 60:.1f} min")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
