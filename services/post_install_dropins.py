"""Cleanup and startup self-heal for systemd drop-ins written by Pantry
packages' ``post_install`` ops.

Packages can ask the root-owned ``/usr/local/sbin/jarvis-post-install``
wrapper to write ``/etc/systemd/system/<svc>.service.d/jarvis.conf``
(e.g. audacy points mpd at a config file inside the package). Those
drop-ins live outside ``~/.jarvis/packages`` and outlive the package
unless something asks the wrapper to remove them.

Incident (jarvis-dev, 2026-10): a factory reset wiped ``~/.jarvis/packages``
without removing audacy's mpd drop-in. ``MPDCONF`` then pointed at a
deleted file and ``Restart=on-failure`` crash-looped mpd every ~5 s
(5,067 restarts, ~4 s CPU each on a Pi Zero 2 W).

This module is deliberately light (stdlib + log client only) so the
provisioning code path (factory reset) can use it.

Privileges: removal goes through the existing sudoers grant
``<user> ALL=(root) NOPASSWD: /usr/local/sbin/jarvis-post-install *``
written by install.sh. Detection only *reads* drop-ins, which are
world-readable (0644). Nothing here needs, or adds, any other privilege.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

WRAPPER_PATH = Path("/usr/local/sbin/jarvis-post-install")
DROPIN_ROOT = Path("/etc/systemd/system")

# Must stay in sync with scripts/jarvis-post-install.
MANAGED_BY_PREFIX = "# managed-by: jarvis-package "
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
# Environment="KEY=VALUE" (the wrapper always quotes).
_ENV_LINE_RE = re.compile(r'^Environment="([^=]+)=(.*)"\s*$')
_CONDITION_RE = re.compile(r"^ConditionPathExists=(/.+?)\s*$")
# A path is "package-shipped" when it lives under a jarvis packages dir —
# exactly the files that disappear when the package (or everything) goes.
_PACKAGE_PATH_MARKER = "/.jarvis/packages/"

_WRAPPER_TIMEOUT_S = 30.0


def _default_packages_dir() -> Path:
    return Path.home() / ".jarvis" / "packages"


def _owner_of(text: str) -> str | None:
    lines = text.splitlines()
    if not lines or not lines[0].startswith(MANAGED_BY_PREFIX):
        return None
    name = lines[0][len(MANAGED_BY_PREFIX):].strip()
    return name if _SAFE_NAME_RE.match(name) else None


def _package_paths(text: str) -> list[tuple[str, str]]:
    """(label, path) for every package-shipped path the drop-in references."""
    out: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        m = _ENV_LINE_RE.match(line)
        if m:
            key, value = m.group(1), m.group(2).replace('\\"', '"').replace("\\\\", "\\")
            if value.startswith("/") and _PACKAGE_PATH_MARKER in value:
                out.append((key, value))
            continue
        c = _CONDITION_RE.match(line)
        if c and _PACKAGE_PATH_MARKER in c.group(1):
            out.append(("ConditionPathExists", c.group(1)))
    return out


def find_stale_managed_dropins(
    dropin_root: Path = DROPIN_ROOT,
    packages_dir: Path | None = None,
) -> dict[str, list[str]]:
    """Return ``{package: [reasons]}`` for package-managed drop-ins that
    should no longer exist.

    A drop-in is stale when its owning package is not installed (no
    ``<packages_dir>/<name>.json``) or when a package-shipped path it
    points the service at (an ``Environment=`` value or
    ``ConditionPathExists=`` under ``~/.jarvis/packages/``) is missing.
    Unmanaged drop-ins (no marker) are never touched.
    """
    packages_dir = packages_dir if packages_dir is not None else _default_packages_dir()
    stale: dict[str, list[str]] = {}
    if not dropin_root.is_dir():
        return stale

    for dropin in sorted(dropin_root.glob("*.service.d/*.conf")):
        try:
            text = dropin.read_text()
        except OSError:
            continue
        owner = _owner_of(text)
        if owner is None:
            continue
        reasons: list[str] = []
        if not (packages_dir / f"{owner}.json").exists():
            reasons.append(f"{dropin}: package {owner!r} is not installed")
        for label, path in _package_paths(text):
            if not Path(path).exists():
                reasons.append(f"{dropin}: {label} points at missing {path}")
        if reasons:
            stale.setdefault(owner, []).extend(reasons)
    return stale


def remove_managed_dropins(package_name: str) -> bool:
    """Ask the sudoers-gated wrapper to remove ``package_name``'s drop-ins.

    Returns True on success. Never raises: uninstall, factory reset and
    startup must all proceed even when the wrapper is absent (dev box,
    macOS) or sudo is not configured.
    """
    if not _SAFE_NAME_RE.match(package_name or ""):
        logger.warning("post_install dropin removal: unsafe package name", package=package_name)
        return False
    if not WRAPPER_PATH.exists():
        return False
    try:
        result = subprocess.run(
            ["sudo", "-n", str(WRAPPER_PATH), "--package", package_name, "remove-managed-dropins"],
            capture_output=True, text=True, timeout=_WRAPPER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        logger.warning("post_install dropin removal skipped", package=package_name, error=str(e))
        return False
    if result.returncode != 0:
        logger.warning(
            "post_install dropin removal failed",
            package=package_name,
            stderr=(result.stderr or "").strip()[:300],
        )
        return False
    out = (result.stdout or "").strip()
    if out:
        logger.info("post_install dropins removed", package=package_name, detail=out[:300])
    return True


def heal_stale_dropins(
    dropin_root: Path = DROPIN_ROOT,
    packages_dir: Path | None = None,
) -> list[str]:
    """Startup self-heal: remove drop-ins left behind by packages that are
    gone (or whose shipped config is gone). Returns the packages healed.
    Never raises."""
    try:
        stale = find_stale_managed_dropins(dropin_root, packages_dir)
    except Exception as e:
        logger.warning("Stale drop-in scan failed (non-fatal)", error=str(e))
        return []
    healed: list[str] = []
    for package, reasons in stale.items():
        logger.warning(
            "Removing stale systemd drop-in left by a removed package",
            package=package,
            reasons="; ".join(reasons)[:500],
        )
        if remove_managed_dropins(package):
            healed.append(package)
        else:
            logger.warning(
                "Could not remove stale drop-in — run "
                f"`sudo {WRAPPER_PATH} --package {package} remove-managed-dropins` on the node",
                package=package,
            )
    return healed
