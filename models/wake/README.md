# Bundled wake-word models

Wake models committed here ship with **every install automatically**: the
release tarball copies `models/` (see `build/build-tarball.sh` SOURCE_DIRS)
and extracts the repo tree to `/opt/jarvis-node`, so a repo model IS the
installed model. Bundled models need no `install.sh:restore_wake_models`
copy-forward and no autodownload — those mechanisms remain only for
package-resident models (openwakeword's site-packages resources dir).

The trained music-robust model (`hey_jarvis_music.onnx`, from the
`tools/wake_model_training` pipeline) lands here once it passes its gates.

## Resolution order (`core/wake_models.py`)

Given the `wake_word_model` setting value `N`:

1. **Bundled** — `<project_root>/models/wake/N.onnx`, if present. Loaded by
   absolute path; the autodownload path is short-circuited (no egress even
   when `wake_word_model_autodownload_enabled` is on).
2. **Package** — otherwise, exactly the previous behavior: openwakeword's
   own resources dir (populated by opt-in autodownload or install.sh
   staging/restore).

Startup logs which source won (structured: `model_name`, `source=bundled|package`,
`path`). All loaders go through the resolver — today the single
construction site is `scripts/voice_listener.py`; the barge-in monitor
(`core/barge_in.py`) shares that same model instance.

## File naming contract

- `<name>.onnx` — **required, primary.** The node runs onnx inference
  (`inference_framework="onnx"`). The basename (minus extension) MUST equal
  the `wake_word_model` setting value: openWakeWord keys its prediction dict
  by basename-without-extension when loading from a path, and the wake loop
  reads `predictions.get(wake_word_model)`. A mismatched basename scores
  under the wrong key and silently never wakes.
- `<name>.tflite` — optional. Not loaded by the node today; include it when
  the training pipeline produces one so other consumers/evals can use it.
- `<name>.metadata.json` — **required sibling** for every committed model:
  - training configuration (pipeline commit/pins, seeds, clip counts,
    SNR buckets, augmentation settings),
  - evaluation numbers (recall per SNR bucket, FA/hr on music-only and
    music+speech negatives, chosen threshold from the sweep),
  - training date,
  - **training-data attribution** — every dataset used, with license.
    When MUSAN is used (it is, for music augmentation), include its
    **CC BY 4.0** attribution (OpenSLR 17; per-subdir LICENSE files carry
    the per-track attributions).

  The training pipeline's `remote_train_wake.py` package stage emits a
  `metadata.json` — commit it alongside the model as `<name>.metadata.json`.

## Deployment gates

Do not commit a model that hasn't passed the deployment gates — recall per
SNR bucket vs stock, quiet-regression bound, false-accept ceilings, June
recordings separation, real-clip corpus. See
[`tools/wake_model_training/README.md`](../../tools/wake_model_training/README.md)
("Deployment gates") for the authoritative list and the threshold-selection
procedure (pick from the `threshold_sweep` table; retune the NOT_FOR_ME
soft-cooldown override alongside).

## Size discipline

Wake models are small (~1 MB class). Keep this directory to models that are
actually deployed or staged for rollout — training artifacts, holdout sets,
and experiment outputs stay out of the repo (see
`tools/wake_model_training/`).
