"""Verify and repair pip dependencies declared by installed Pantry packages.

Why this exists
---------------
The kitchen-node beta-test (May 2026) surfaced two ways a Pantry
package's declared pip deps can go missing from the venv:

  1. Original install: the package's apt step ran successfully but its
     pip step skipped/failed, leaving metadata claiming "installed"
     while the dep was actually missing.
  2. Node self-update: ``install.sh:rebuild_venv()`` blows away
     ``/opt/jarvis-node/.venv`` and rebuilds from
     ``requirements-pi.txt`` only — Pantry-installed pip deps aren't
     in any requirements file, so every node update silently wipes
     them.

Either way the first symptom is the user trying to play music and
hearing TTS say nothing while the log quietly carries

  Music play failed | error='music-assistant-client is not installed.
                             Install it with: pip install music-assistant-client'

The self-heal walks ``~/.jarvis/packages/*.json`` on every node
startup. Each metadata file carries a ``pip_packages`` field
(populated by ``command_store_service._write_package_metadata`` starting
in v0.1.67). For each declared dep, we verify the distribution exists
in the running venv and pip-install anything that's missing. On a
healthy node this is a one-shot ``pip list`` and nothing else.

Legacy installs (pre-v0.1.67 metadata without ``pip_packages``) are
skipped with a single info log — re-installing the package via the
mobile app refreshes the metadata.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Iterable

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


_PACKAGES_DIR = Path.home() / ".jarvis" / "packages"


def _installed_distribution_names() -> set[str]:
    """Return the set of distribution names currently importable in the
    venv (normalised to lowercase, hyphens preserved). Empty on failure.

    Uses ``pip list --format=json`` rather than ``importlib.metadata``
    so we don't pay the import cost during startup if the venv is large
    — pip ships the list in <100ms typically.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--format=json"],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("pip list failed during pantry self-heal", error=str(e))
        return set()
    if result.returncode != 0:
        logger.warning("pip list non-zero exit", stderr=result.stderr[:200])
        return set()
    try:
        entries = json.loads(result.stdout)
    except (ValueError, TypeError):
        return set()
    return {str(e.get("name", "")).lower() for e in entries if e.get("name")}


def _missing_pip_deps_for_package(
    metadata: dict, installed: set[str],
) -> list[dict]:
    """Return the pip_packages entries from ``metadata`` that aren't
    installed. Empty list when nothing is missing or the metadata
    doesn't declare pip_packages (legacy install, pre-v0.1.67)."""
    declared = metadata.get("pip_packages") or []
    if not isinstance(declared, list):
        return []
    missing: list[dict] = []
    for pkg in declared:
        name = (pkg.get("name") or "").lower()
        if not name:
            continue
        if name not in installed:
            missing.append(pkg)
    return missing


def _pip_install(specs: Iterable[str]) -> bool:
    """Run pip install on ``specs``. Returns True if pip exited 0."""
    spec_list = list(specs)
    if not spec_list:
        return True
    logger.info("Self-heal pip install", packages=spec_list)
    try:
        result = subprocess.run(
            ["nice", "-n", "15", sys.executable, "-m", "pip", "install",
             "--quiet", "--prefer-binary"] + spec_list,
            capture_output=True, text=True, timeout=600,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error("Self-heal pip install raised", error=str(e))
        return False
    if result.returncode != 0:
        logger.error(
            "Self-heal pip install failed",
            packages=spec_list,
            stderr=result.stderr.strip()[:500],
        )
        return False
    return True


def _format_spec(pkg: dict) -> str:
    """Format a pip_packages entry as a pip install spec string."""
    name = pkg.get("name", "")
    version = pkg.get("version") or ""
    if version:
        if version[0].isdigit():
            return f"{name}=={version}"
        return f"{name}{version}"
    return name


def verify_pantry_pip_deps() -> int:
    """Verify pip deps for every installed Pantry package and reinstall
    any that are missing.

    Walks ``~/.jarvis/packages/*.json``. For each package with a
    ``pip_packages`` field, checks that each declared dep is present
    in the running venv and pip-installs anything that isn't. Returns
    the count of packages that needed repair (zero on a healthy node).

    No-op on a node with no installed Pantry packages.
    """
    if not _PACKAGES_DIR.exists():
        return 0
    meta_files = sorted(_PACKAGES_DIR.glob("*.json"))
    if not meta_files:
        return 0

    installed = _installed_distribution_names()
    if not installed:
        # Couldn't read pip list — abandon rather than false-alarm on
        # every package.
        logger.warning("Skipping pantry self-heal: pip list unavailable")
        return 0

    packages_repaired = 0
    legacy_skipped = 0
    for meta_file in meta_files:
        try:
            metadata = json.loads(meta_file.read_text())
        except (OSError, ValueError) as e:
            logger.warning(
                "Failed to read Pantry metadata",
                file=str(meta_file), error=str(e),
            )
            continue
        if "pip_packages" not in metadata:
            legacy_skipped += 1
            continue
        missing = _missing_pip_deps_for_package(metadata, installed)
        if not missing:
            continue
        package_name = metadata.get("package_name", meta_file.stem)
        specs = [_format_spec(p) for p in missing]
        logger.warning(
            "Pantry package missing declared pip dependencies",
            package=package_name,
            missing=specs,
        )
        if _pip_install(specs):
            packages_repaired += 1
            logger.info(
                "Self-heal restored pip deps", package=package_name, specs=specs,
            )

    if legacy_skipped:
        logger.info(
            "Pantry self-heal skipped legacy packages (no pip_packages metadata)",
            count=legacy_skipped,
            hint="reinstall via mobile to refresh metadata",
        )
    return packages_repaired
