"""Unified dependency resolver for jarvis-node-setup commands.

Collects ``required_packages`` from all enabled commands, merges them with
base requirements, and detects version conflicts using the ``packaging``
library.  Pure logic — no side effects (pip installs, file writes) happen
here; the caller (``install_command.py``) decides what to do with the
:class:`ResolutionResult`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from packaging.specifiers import SpecifierSet
from packaging.version import Version


# ── Helpers ──────────────────────────────────────────────────────────

_NORMALIZE_RE = re.compile(r"[-_.]+")


def _normalize(name: str) -> str:
    """Normalize a package name to lowercase-hyphen form (PEP 503)."""
    return _NORMALIZE_RE.sub("-", name).lower()


def _version_spec_from_jarvis(version: str | None) -> str:
    """Convert a JarvisPackage.version to a pip specifier string."""
    if not version:
        return ""
    # If the version starts with a digit it's a pin (e.g. "1.0.0" → "==1.0.0")
    if version[0].isdigit():
        return f"=={version}"
    return version


# ── Data classes ─────────────────────────────────────────────────────


@dataclass
class DependencyConflict:
    package_name: str
    sources: list[tuple[str, str]]  # [(source_name, version_spec), ...]
    reason: str


@dataclass
class ResolutionResult:
    success: bool
    merged_specs: list[str]  # pip specs for custom-requirements.txt
    conflicts: list[DependencyConflict] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)


# ── Public API ───────────────────────────────────────────────────────


def parse_requirements_file(path: str) -> dict[str, str]:
    """Parse a requirements.txt into ``{normalized_name: version_spec}``.

    Skips comments, blank lines, and URL-based dependencies (``@ git+…``
    or ``@ https://…``).
    """
    result: dict[str, str] = {}
    with open(path) as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Skip URL-based deps (e.g. "pkg @ git+https://..." or "pkg @ https://...")
            if " @ " in line:
                continue

            # Split name from version spec.  First operator char wins.
            match = re.match(r"^([A-Za-z0-9_.\-]+)(.*)", line)
            if not match:
                continue
            name = _normalize(match.group(1))
            spec = match.group(2).strip()
            result[name] = spec
    return result


def collect_command_packages(
    commands: dict[str, Any],
) -> dict[str, list[tuple[str, str]]]:
    """Gather ``required_packages`` from every command, grouped by
    normalized package name.

    Returns ``{pkg_name: [(command_name, version_spec), ...]}``.
    """
    grouped: dict[str, list[tuple[str, str]]] = {}
    for cmd in commands.values():
        for pkg in cmd.required_packages:
            key = _normalize(pkg.name)
            spec = _version_spec_from_jarvis(pkg.version)
            grouped.setdefault(key, []).append((cmd.command_name, spec))
    return grouped


def check_compatibility(specs: list[str]) -> bool:
    """Return True if all *specs* can be simultaneously satisfied.

    Strategy: merge into one :class:`SpecifierSet` and test a range of
    synthetic versions.  Good enough for an MVP — catches obvious
    conflicts (disjoint ranges, conflicting pins).
    """
    if not specs:
        return True

    # Filter out empty (unconstrained) specs
    non_empty = [s for s in specs if s]
    if not non_empty:
        return True

    merged = SpecifierSet()
    for s in non_empty:
        merged &= SpecifierSet(s)

    # Test synthetic versions: 0.1 .. 100.0 in steps of 0.1
    candidates = [Version(f"{major}.{minor}.0") for major in range(101) for minor in range(10)]
    return any(v in merged for v in candidates)


def resolve_all(
    base_req_path: str,
    commands: dict[str, Any],
) -> ResolutionResult:
    """Run full dependency resolution.

    1. Parse base requirements.
    2. Collect command packages.
    3. For each command package, check compatibility (across commands and
       against base).
    4. Build ``merged_specs`` (only new packages, not already in base).
    """
    base_reqs = parse_requirements_file(base_req_path)
    cmd_packages = collect_command_packages(commands)

    conflicts: list[DependencyConflict] = []
    merged_specs: list[str] = []

    for pkg_name, sources in sorted(cmd_packages.items()):
        all_specs: list[str] = [spec for _, spec in sources]
        all_sources: list[tuple[str, str]] = list(sources)

        in_base = pkg_name in base_reqs
        if in_base:
            base_spec = base_reqs[pkg_name]
            all_specs.append(base_spec)
            all_sources.append(("requirements.txt", base_spec))

        if not check_compatibility(all_specs):
            conflicts.append(DependencyConflict(
                package_name=pkg_name,
                sources=all_sources,
                reason=_conflict_reason(all_specs),
            ))
            continue

        # If it's already in base and compatible, base wins — skip.
        if in_base:
            continue

        # Merge all non-empty specs into a single pip line.
        non_empty = [s for s in all_specs if s]
        if non_empty:
            # Combine all specifier fragments (e.g. ">=1.0" + "<2.0" → ">=1.0,<2.0")
            combined = ",".join(non_empty)
            merged_specs.append(f"{pkg_name}{combined}")
        else:
            merged_specs.append(pkg_name)

    # Build snapshot
    snapshot = _build_snapshot(commands, cmd_packages, merged_specs)

    return ResolutionResult(
        success=len(conflicts) == 0,
        merged_specs=sorted(merged_specs),
        conflicts=conflicts,
        snapshot=snapshot,
    )


# ── Private helpers ──────────────────────────────────────────────────


def _conflict_reason(specs: list[str]) -> str:
    non_empty = [s for s in specs if s]
    return f"No version satisfies all constraints: {', '.join(non_empty)}"


def _build_snapshot(
    commands: dict[str, Any],
    cmd_packages: dict[str, list[tuple[str, str]]],
    merged_specs: list[str],
) -> dict[str, Any]:
    """Build the dependency-snapshot.json payload."""
    cmd_snapshot: dict[str, list[str]] = {}
    for cmd in commands.values():
        pkgs = cmd.required_packages
        if pkgs:
            cmd_snapshot[cmd.command_name] = [
                p.to_pip_spec() for p in pkgs
            ]

    return {
        "resolved_at": datetime.now(timezone.utc).isoformat(),
        "commands": cmd_snapshot,
        "merged_specs": merged_specs,
    }
