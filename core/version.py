"""Runtime version introspection for a jarvis-node install.

Source-of-truth is the VERSION file written by build/build-tarball.sh. On
a tarball install that's `/opt/jarvis-node/VERSION`. For Docker, the
Dockerfile should COPY the same VERSION file into /app. For a dev checkout
(no VERSION file) we fall back to `git describe`.

All fields are reported in the heartbeat so the Command Center can tell a
node apart from its peers and the mobile app can show "update available"
without polling the node directly.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass
class VersionInfo:
    version: str
    install_mode: str  # "tarball" | "docker" | "dev"
    git_sha: str | None
    install_dir: str | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_version_file(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, PermissionError, OSError):
        return None
    return text or None


def _git_describe(cwd: Path) -> tuple[str | None, str | None]:
    try:
        describe = subprocess.check_output(
            ["git", "describe", "--tags", "--always", "--dirty"],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        sha = subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=cwd,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        ).strip()
        return describe or None, sha or None
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None, None


def _detect_install_mode() -> str:
    if os.path.exists("/.dockerenv"):
        return "docker"
    if os.path.exists("/opt/jarvis-node/VERSION"):
        return "tarball"
    return "dev"


def version_info() -> VersionInfo:
    """Return this node's version info, built fresh each call so a rebuild
    of the service picks up the new VERSION file without a cold start."""
    install_mode = _detect_install_mode()

    # Tarball install: /opt/jarvis-node/VERSION is authoritative.
    if install_mode == "tarball":
        v = _read_version_file(Path("/opt/jarvis-node/VERSION"))
        return VersionInfo(
            version=v or "unknown",
            install_mode="tarball",
            git_sha=None,
            install_dir="/opt/jarvis-node",
        )

    # Docker: VERSION is baked into the image at /app/VERSION.
    if install_mode == "docker":
        v = _read_version_file(Path("/app/VERSION"))
        return VersionInfo(
            version=v or "unknown",
            install_mode="docker",
            git_sha=None,
            install_dir="/app",
        )

    # Dev: best-effort git describe from this file's repo.
    repo = Path(__file__).resolve().parent.parent
    describe, sha = _git_describe(repo)
    return VersionInfo(
        version=describe or "dev",
        install_mode="dev",
        git_sha=sha,
        install_dir=str(repo),
    )
