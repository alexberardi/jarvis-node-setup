"""Wake-model resolution — bundled repo models first, then package.

Bundled models live at ``<project_root>/models/wake/<name>.onnx`` and ship
with the code itself: the release tarball copies ``models/`` (see
``build/build-tarball.sh`` SOURCE_DIRS) and extracts the repo tree to
``/opt/jarvis-node``, so a repo model IS the installed model. That kills
the fragile venv-resident dance for bundled models — no
``install.sh:restore_wake_models`` copy-forward, no autodownload, no
"venv rebuild silently ate the wake model" failure class.

Package-resident models (openwakeword's ``site-packages`` resources dir,
populated by opt-in autodownload or install.sh staging) remain the
fallback with their exact previous behavior.

Every openWakeWord loader must go through :func:`prepare_wake_model` so
all detectors score the same model. Today there is exactly one
construction site — ``scripts.voice_listener.start_voice_listener`` —
and the barge-in monitor (``core/barge_in.py``) deliberately shares that
instance (one model per node; there is no CPU budget for two).

Naming contract (documented in ``models/wake/README.md``): the file
basename must equal the ``wake_word_model`` setting value, because
openWakeWord keys its prediction dict by basename-without-extension when
loading from a path — ``models/wake/hey_jarvis.onnx`` scores under
``"hey_jarvis"``, exactly like the package-resident model, so downstream
``predictions.get(wake_word_model)`` lookups are source-agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from jarvis_log_client import JarvisLogger


logger = JarvisLogger(service="jarvis-node")

# <project_root>/models/wake — models/ is the DB-models Python package;
# wake/ is a plain data subdir inside it (non-importable, just files).
BUNDLED_WAKE_MODEL_DIR = (
    Path(__file__).resolve().parent.parent / "models" / "wake"
)

SOURCE_BUNDLED = "bundled"
SOURCE_PACKAGE = "package"


@dataclass(frozen=True)
class ResolvedWakeModel:
    """Where a wake model was found and how to hand it to openWakeWord."""

    name: str
    source: str  # SOURCE_BUNDLED | SOURCE_PACKAGE
    path: str | None = None  # absolute path when bundled, else None

    @property
    def model_ref(self) -> str:
        """Value to put in openWakeWord ``Model``'s model-list argument.

        Bundled → absolute file path; package → the bare model name
        (openWakeWord resolves names against its own resources dir),
        which is byte-for-byte the pre-resolver behavior.
        """
        return self.path if self.path is not None else self.name


def resolve_wake_model(
    name: str, bundled_dir: Path | None = None
) -> ResolvedWakeModel:
    """Resolve ``name`` → bundled repo model if present, else package.

    Pure lookup, no side effects — ``bundled_dir`` is injectable for
    tests only.
    """
    directory = (
        bundled_dir if bundled_dir is not None else BUNDLED_WAKE_MODEL_DIR
    )
    candidate = directory / f"{name}.onnx"
    if candidate.is_file():
        return ResolvedWakeModel(
            name=name, source=SOURCE_BUNDLED, path=str(candidate)
        )
    return ResolvedWakeModel(name=name, source=SOURCE_PACKAGE)


def prepare_wake_model(
    name: str,
    *,
    autodownload_enabled: bool,
    bundled_dir: Path | None = None,
) -> ResolvedWakeModel:
    """Resolve ``name`` and stage it for loading. The one startup entry point.

    Bundled models short-circuit the autodownload path entirely — they
    ship with the install, so no egress happens even when
    ``wake_word_model_autodownload_enabled`` is on. Package-resident
    models keep the exact previous consent-gated behavior: download only
    when explicitly enabled, otherwise load whatever is already staged
    (and let the caller's ``Model(...)`` raise into its existing
    missing-model fallback).
    """
    resolved = resolve_wake_model(name, bundled_dir=bundled_dir)
    if resolved.source == SOURCE_BUNDLED:
        logger.info(
            "Wake model resolved",
            model_name=resolved.name,
            source=resolved.source,
            path=resolved.path,
        )
        return resolved

    if autodownload_enabled:
        # Lazy import: keeps this module importable without the audio /
        # onnx stack (same reason core/voice_filters.py exists).
        import openwakeword.utils

        openwakeword.utils.download_models(model_names=[name])
    else:
        logger.info(
            "Skipping openWakeWord model download (autodownload disabled by "
            "policy) — loading locally staged model only",
            model=name,
        )
    logger.info(
        "Wake model resolved",
        model_name=resolved.name,
        source=resolved.source,
        path=resolved.path,
    )
    return resolved
