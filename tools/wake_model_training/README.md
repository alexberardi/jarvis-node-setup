# Music-Robust Wake Model Training Pipeline

Trains `hey_jarvis_music` — a custom openWakeWord model for "hey jarvis"
that keeps working while music plays. Built to eventually **replace** the
stock `hey_jarvis` model on the fleet.

## Why

June 2026 kitchen measurements (`prds/wake-during-music/`): with music at
kitchen volume, stock `hey_jarvis` OWW scores **cap at 0.14–0.43 on real
wake phrases** (speaker-bleed-only band: 0.10–0.18). No threshold fixes
that — the scores overlap the bleed band. The fix is a model that has
actually *seen* speech-over-music during training. The volume-duck /
self-playback work on `feat/music-duck` mitigates; this pipeline is the
structural fix.

**One model per node.** OWW inference costs 60–80 ms of the node's 80 ms
per-chunk budget on the Pi Zero. There is no room for dual-model scoring —
`hey_jarvis_music` replaces stock outright after it passes the gates below.

## Pipeline

```
generate_positives.py      5000 synthetic "hey jarvis" clips (piper-sample-
        │                  generator, 904-voice LibriTTS-R checkpoint)
        ▼                  + adversarial near-miss clips ("hey travis", ...)
augment_music.py           mix positives with MUSAN music at SNR +10 → -10 dB,
        │                  optional MIT-RIR kitchen reverb; build music-only
        │                  and music+speech NEGATIVES; reserve a 15% eval
        │                  holdout that training never sees
        ▼
train_runpod.py ──ssh──▶ remote_train_wake.py     (on a RunPod 3090/4090)
        │                  merge clips into openWakeWord's layout, run
        │                  train.py --augment_clips / --train_model
        ▼
artifacts/                 hey_jarvis_music.onnx + .tflite + metadata.json
        ▼
evaluate.py                candidate vs STOCK hey_jarvis on the same held-out
                           sets → the deployment decision
```

All sources are **pinned in `common.py`**: openWakeWord
`368c0371` (main @ 2025-12-30), piper-sample-generator `2971426a`
(master @ 2026-03-12) + its v2.0.0 `en_US-libritts_r-medium.pt` checkpoint,
openWakeWord's published negative-feature `.npy` files (HF
`davidscripka/openwakeword_features`), MUSAN (OpenSLR 17, **CC BY 4.0** —
per-subdir LICENSE files carry attribution), MIT environmental impulse
responses (HF `davidscripka/MIT_environmental_impulse_responses`).

## Runbook

```bash
cd tools/wake_model_training

# 0. Sanity: unit tests + dry runs (no downloads, no pod)
pytest test_augment_music.py -v
python generate_positives.py --dry-run
python augment_music.py --dry-run --positives-dir ./data/positives \
    --music-dir ./data/musan/music --out-dir ./data/augmented
python train_runpod.py --dry-run
python remote_train_wake.py --dry-run

# 1. Full cloud run (provisions pod → trains → downloads → terminates)
python train_runpod.py --api-key $RUNPOD_API_KEY

#    Partial/resume: --pod-id <id> --stages features,train,package
#    Keep the pod for debugging: --keep-pod

# 2. Pull the eval holdout down from the pod (or regenerate stages
#    positives+augment locally — deterministic via --seed) and evaluate:
python evaluate.py \
    --model artifacts/hey_jarvis_music.onnx \
    --eval-dir ./data/augmented/eval_holdout \
    --compare-stock \
    --june-recordings ../../prds/wake-during-music/recordings \
    --report artifacts/eval_report.json
```

Local dev (no pod) also works: run `generate_positives.py` (CPU TTS is
slow but fine overnight) and `augment_music.py` on the laptop, then rent
the pod only for `--stages features,train,package`.

### Cost

**~$5–15 total.** openWakeWord's trainable head is a small DNN over frozen
embedding features — it trains on modest GPUs. A community RTX 4090/3090
(~$0.30–0.70/hr) for 3–8 hrs wall time; most of that is TTS generation and
augmentation/feature extraction, not the training loop. No H100 needed
(don't copy the date-adapter harness's GPU choice).

### Eval data

- **`eval_holdout/`** — 15% of the music-mixed positives and negatives,
  reserved by `augment_music.py`, never merged into training.
- **June 2026 recordings** (`prds/wake-during-music/recordings/`) — 5 files,
  ~65 s, 48 kHz stereo, real mic + real kitchen. **Eval only, never
  training**: it's far too small to train on, and it's the only
  real-microphone ground truth we have.
- **Real wake-clip corpus** — since node v0.2.4 every wake fire writes the
  consumed-chunks clip (~2 s, 16 kHz mono) and CC verification issues a
  verdict. Export as `clips/*.wav` + `labels.jsonl`
  (`{"file", "verdict": "confirmed"|"false_wake", "oww_score",
  "self_playback", ...}`) and pass `--real-clips-dir`. Score-biased
  (only above-threshold fires produce clips), so it yields per-clip score
  deltas vs stock, not absolute recall.

## Deployment gates

Ship `hey_jarvis_music` only if **all** hold (from
`artifacts/eval_report.json`, candidate vs stock at the same threshold):

1. **Recall ≥ stock at every SNR bucket** (+10, +5, 0, -5, -10 dB), and
   **recall ≥ 0.75 at 0 dB and ≥ 0.5 at -5 dB** — the regime stock
   effectively scores 0 in (score cap 0.43 < typical threshold).
2. **No regression in quiet**: recall on clean positives (+10 dB bucket
   proxies this) within 2 points of stock.
3. **False-accept ceiling**: ≤ 0.5 FA/hr on music-only negatives and
   ≤ 1 FA/hr on music+speech negatives, and **no worse than stock** on
   both. (Per the fail-open doctrine, CC clip verification absorbs some
   extra false fires — but a model that sprays on bare music torches the
   telemetry that lets us peel layers back, so it gates here.)
4. **June recordings**: candidate max-score on `voice_music_mic.wav`
   clearly above its score on `music_only_mic.wav` (stock's separation is
   ~0.43 vs ~0.18 — candidate must beat that margin).
5. **Real-clip corpus** (once ≥ ~100 labeled clips): median candidate
   score on `verdict=confirmed` clips ≥ stock's, and no new
   above-threshold mass on `verdict=false_wake` clips.

Pick the deployment threshold from the `threshold_sweep` table, not by
inheriting the stock threshold — then re-check gates at that threshold.
Note the NOT_FOR_ME soft-cooldown override (0.6) and the June loud-music
score physics both assume stock's score distribution; retune those
settings alongside the threshold when deploying.

## Distribution (not built here — existing mechanisms)

- The node selects its model via the **`wake_word_model` setting**
  (default `hey_jarvis`) — set it to `hey_jarvis_music` per node for the
  staged rollout (dev node → kitchen).
- **Bundled distribution (`models/wake/`)**: commit
  `hey_jarvis_music.onnx` (+ `.tflite` + `hey_jarvis_music.metadata.json`)
  to `models/wake/` — see `models/wake/README.md` for the naming/metadata
  contract. `core/wake_models.py` resolves bundled models first, the
  release tarball includes `models/`, and the repo tree IS the install
  tree at `/opt/jarvis-node` — so the model ships with every install
  automatically, with no venv-resident copy, no
  `install.sh:restore_wake_models` dance, and no autodownload. (Those
  mechanisms remain only for package-resident stock models.)
- The per-fire "Wake fired" structured log already carries
  `oww_score` / `self_playback` / `vad_threshold_source`; after rollout the
  same telemetry compares the models in production. Tag the deployed model
  name in any new telemetry you add.

## Files

| File | Role |
|---|---|
| `common.py` | Pinned repos/datasets/URLs, WAV I/O, SNR math (unit-tested) |
| `generate_positives.py` | Synthetic positives + adversarial near-misses |
| `augment_music.py` | SNR mixing, negatives, eval holdout, manifest |
| `train_runpod.py` | Local orchestrator: pod up → train → download → terminate |
| `remote_train_wake.py` | On-pod: setup → clips → merge → features → train → package |
| `evaluate.py` | Candidate-vs-stock report; the deployment decision |
| `test_augment_music.py` | Unit tests for SNR math + WAV plumbing |
| `requirements-remote.txt` | Pod dependencies |

`data/`, `artifacts/`, and checkouts of the pinned repos are gitignored —
everything regenerable is regenerated, only code + pins are committed.
