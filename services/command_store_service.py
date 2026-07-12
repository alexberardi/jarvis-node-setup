"""Local command store operations: install, remove, update, list.

Manages custom commands/bundles installed from GitHub repos or the command
store API. Single commands install to commands/custom_commands/<name>/.
Bundles scatter components to their type-specific directories.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jarvis_log_client import JarvisLogger

from core.command_manifest import CommandManifest

logger = JarvisLogger(service="jarvis-node")

# Base project dir (jarvis-node-setup/)
_PROJECT_DIR = Path(__file__).resolve().parent.parent

# Component type → install directory relative to project root
COMPONENT_INSTALL_DIRS: dict[str, str] = {
    "command": "commands/custom_commands",
    "agent": "agents/custom_agents",
    "device_protocol": "device_families/custom_families",
    "device_manager": "device_managers/custom_managers",
    "routine": "routines/custom_routines",
}

# Path to custom commands directory
CUSTOM_COMMANDS_DIR = _PROJECT_DIR / "commands" / "custom_commands"

# Package metadata stored in ~/.jarvis/packages/
PACKAGES_DIR = Path.home() / ".jarvis" / "packages"

# Metadata file written alongside installed commands
STORE_METADATA_FILE = ".store_metadata.json"


def register_package_lib_paths() -> None:
    """Add all installed package lib dirs to sys.path.

    Call this at node startup so scattered components can import shared
    code from their bundle's lib directory. Also adds PACKAGES_DIR itself
    so auto-generated package namespaces are importable (e.g. ``from nest import NestProtocol``).
    """
    if not PACKAGES_DIR.exists():
        return

    # Add PACKAGES_DIR so auto-generated package namespaces are importable
    if str(PACKAGES_DIR) not in sys.path:
        sys.path.append(str(PACKAGES_DIR))

    for meta_file in PACKAGES_DIR.glob("*.json"):
        package_name = meta_file.stem
        # New convention: ~/.jarvis/packages/<name>/<name>_lib/.
        # Legacy installs (pre-rename) have shared code at ~/.jarvis/packages/<name>/lib/;
        # keep scanning both so an existing install isn't silently broken
        # the next time the node restarts after the convention change.
        for candidate_name in (f"{package_name}_lib", "lib"):
            lib_dir = meta_file.parent / package_name / candidate_name
            if lib_dir.is_dir() and str(lib_dir) not in sys.path:
                sys.path.append(str(lib_dir))
                logger.debug("Added package lib to path", package=package_name, lib=str(lib_dir))


class CommandStoreError(Exception):
    """Base exception for command store operations."""


class InstallError(CommandStoreError):
    """Error during command installation."""


class RemoveError(CommandStoreError):
    """Error during command removal."""


class RevertError(CommandStoreError):
    """Error during package revert (restore of the .previous snapshot)."""


def _parse_semver(s: str) -> tuple[int, int, int]:
    """Parse a loose semver string into a comparable (major, minor, patch).

    Strips a leading "v", splits on ".", coerces each part int-or-0, and
    pads to three parts. Total — never raises. Garbage input collapses to
    (0, 0, 0); callers treat that as unparseable and skip their check
    rather than block an install on bad metadata.
    """
    text = (s or "").strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    # Drop git-describe / pre-release / build suffixes ("0.1.97-12-gabc1234
    # -dirty", "1.2.3+build") before splitting on "." — otherwise the suffix
    # corrupts the patch part ("114-dirty" → 0) and a node running a dirty
    # checkout would fail floors it actually meets.
    text = text.split("-", 1)[0].split("+", 1)[0]
    nums: list[int] = []
    for part in text.split(".")[:3]:
        try:
            nums.append(int(part))
        except (TypeError, ValueError):
            nums.append(0)
    while len(nums) < 3:
        nums.append(0)
    return (nums[0], nums[1], nums[2])


def _node_version_str() -> str:
    """This node's version string (from core.version). Test seam."""
    from core.version import version_info
    return version_info().version


def _sdk_version_str() -> str:
    """Installed jarvis_command_sdk version string. Test seam."""
    try:
        import jarvis_command_sdk
        return str(getattr(jarvis_command_sdk, "__version__", "") or "")
    except Exception:
        return ""


def _enforce_version_floors(manifest: CommandManifest) -> None:
    """Refuse the install when the manifest declares a version floor this
    node doesn't meet. Runs BEFORE any disk writes.

    Missing or unparseable floors skip the check with a logged warning —
    bad metadata must not block installs. An unparseable node/SDK version
    (dev checkouts can report "dev"/"unknown") skips too.
    """
    checks: list[tuple[str, str | None, str, str, str]] = [
        # (floor field, floor value, current version, noun, "is"/"has" phrasing)
        ("min_jarvis_version", getattr(manifest, "min_jarvis_version", None),
         _node_version_str(), "node version", "is"),
        ("min_sdk_version", getattr(manifest, "min_sdk_version", None),
         _sdk_version_str(), "SDK version", "has SDK"),
    ]

    for field, floor_str, current_str, noun, verb in checks:
        if not floor_str:
            continue

        # ~28 published packages carry the scaffold default "0.9.0", which
        # predates node versioning and floor enforcement — it carries no
        # signal, and 0.1.x nodes would block every install. Treat it as
        # no-floor.
        if field == "min_jarvis_version" and floor_str.strip().lstrip("vV") == "0.9.0":
            logger.warning(
                "Legacy scaffold placeholder floor — skipping check",
                package=manifest.name,
            )
            continue

        floor = _parse_semver(floor_str)
        if floor == (0, 0, 0):
            logger.warning(
                "Unparseable version floor — skipping check",
                field=field, value=floor_str, package=manifest.name,
            )
            continue

        current = _parse_semver(current_str)
        if current == (0, 0, 0):
            logger.warning(
                "Unparseable installed version — skipping floor check",
                field=field, current=current_str, package=manifest.name,
            )
            continue

        if floor > current:
            # The SDK ships with the node, so both floors resolve to the
            # same user action: update the node.
            raise InstallError(
                f"This package needs {noun} {floor_str} or newer — "
                f"this node {verb} {current_str}. Update the node first, "
                f"then retry."
            )


def _download_archive(repo_url: str, tag: str | None = None) -> Path | None:
    """Download a GitHub repo as a tarball (no git/auth required).

    Returns path to extracted repo dir, or None if download fails.
    """
    import urllib.request
    import urllib.error

    # Convert https://github.com/owner/repo to archive URL
    clean = repo_url.rstrip("/").removesuffix(".git")
    ref = tag or "main"
    archive_url = f"{clean}/archive/{ref}.tar.gz"

    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cmd-"))
    try:
        with urllib.request.urlopen(archive_url, timeout=60) as resp:
            data = resp.read()
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(path=str(tmpdir), filter="data")

        # GitHub archives extract to {repo}-{ref}/ — find the single directory
        subdirs = [d for d in tmpdir.iterdir() if d.is_dir()]
        if len(subdirs) == 1:
            return subdirs[0]
        return None
    except (urllib.error.URLError, tarfile.TarError, OSError) as e:
        logger.warning("Archive download failed, will try git clone", error=str(e))
        shutil.rmtree(tmpdir, ignore_errors=True)
        return None


def _looks_like_commit_sha(ref: str) -> bool:
    """A ref that is a 7-40 char lowercase hex string is a commit SHA, not a
    branch/tag name. Pantry now pins installs to the immutable commit it
    validated (a tag can be force-repointed to un-reviewed code after review),
    and ``git clone --branch`` rejects a raw SHA — so we must fetch these by
    object id rather than silently falling back to the default branch.
    """
    ref = ref.strip()
    return 7 <= len(ref) <= 40 and all(c in "0123456789abcdef" for c in ref.lower())


def _clone_repo(repo_url: str, tag: str | None = None) -> Path:
    """Download a repo — tries archive download first, falls back to git.

    Args:
        repo_url: GitHub HTTPS URL.
        tag: Optional git ref to fetch — a tag/branch name OR a commit SHA
            (Pantry pins to the validated commit SHA to defeat the
            validate-then-repoint TOCTOU).

    Returns:
        Path to the repo directory.
    """
    # Try archive download first (no git/auth needed). GitHub serves
    # /archive/<sha>.tar.gz for a full commit SHA, so this path already pins
    # exactly what Pantry validated.
    result_path = _download_archive(repo_url, tag)
    if result_path:
        return result_path

    # Prevent git from prompting for credentials (hangs headless nodes)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cmd-"))
    repo_path = tmpdir / "repo"

    # A commit SHA can't be `git clone --branch`ed. Fetch the exact object so a
    # pinned install NEVER silently degrades to the mutable default branch (which
    # would reintroduce the TOCTOU the SHA pin closes). GitHub allows fetching an
    # arbitrary reachable SHA.
    if tag and _looks_like_commit_sha(tag):
        steps = [
            ["git", "init", "--quiet", str(repo_path)],
            ["git", "-C", str(repo_path), "remote", "add", "origin", repo_url],
            ["git", "-C", str(repo_path), "fetch", "--depth", "1", "origin", tag],
            ["git", "-C", str(repo_path), "checkout", "--quiet", "FETCH_HEAD"],
        ]
        for step in steps:
            result = subprocess.run(step, capture_output=True, text=True, timeout=60, env=env)
            if result.returncode != 0:
                shutil.rmtree(tmpdir, ignore_errors=True)
                raise InstallError(
                    f"Failed to fetch pinned commit {tag}: {result.stderr.strip()}"
                )
        return repo_path

    cmd = ["git", "clone", "--depth", "1"]
    if tag:
        cmd.extend(["--branch", tag])
    cmd.extend([repo_url, str(repo_path)])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if result.returncode != 0 and tag:
        shutil.rmtree(tmpdir, ignore_errors=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cmd-"))
        repo_path = tmpdir / "repo"
        fallback_cmd = ["git", "clone", "--depth", "1", repo_url, str(repo_path)]
        result = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=60, env=env)
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise InstallError(f"Failed to download package: {result.stderr.strip()}")

    return repo_path


def _validate_repo_structure(repo_dir: Path) -> CommandManifest:
    """Validate the repo has required files and a valid manifest.

    Supports both single-command repos (jarvis_command.yaml + command.py)
    and bundle repos (jarvis_package.yaml with components list).

    Returns:
        Parsed CommandManifest.
    """
    # Find manifest file
    manifest_path: Path | None = None
    for name in ("jarvis_package.yaml", "jarvis_command.yaml"):
        if (repo_dir / name).exists():
            manifest_path = repo_dir / name
            break

    if manifest_path is None:
        raise InstallError("Missing manifest: neither jarvis_package.yaml nor jarvis_command.yaml found")

    with open(manifest_path) as f:
        raw = yaml.safe_load(f)

    try:
        manifest = CommandManifest(**raw)
    except Exception as e:
        raise InstallError(f"Invalid manifest: {e}")

    if not manifest.name:
        raise InstallError("Manifest 'name' is empty")

    # Infer components from repo structure if not declared
    if not manifest.components:
        from core.command_manifest import infer_components
        manifest.components = infer_components(repo_dir, manifest.name)
        if not manifest.components:
            raise InstallError(
                "No components found. Declare 'components' in the manifest or use "
                "the convention: command.py, commands/*/command.py, agents/*/agent.py, "
                "device_families/*/protocol.py, device_managers/*/manager.py"
            )

    # Verify each component path exists
    for comp in manifest.components:
        comp_path = repo_dir / comp.path
        if not comp_path.exists():
            raise InstallError(f"Component '{comp.name}' path not found: {comp.path}")

    return manifest


def _check_platform_compatibility(manifest: CommandManifest) -> None:
    """Check if the command supports the current platform."""
    import platform

    if manifest.platforms:
        current = platform.system().lower()
        if current not in [p.lower() for p in manifest.platforms]:
            raise InstallError(
                f"Command requires platforms {manifest.platforms}, "
                f"but current platform is '{current}'"
            )


def _check_name_conflicts(manifest: CommandManifest) -> None:
    """Check if any component name conflicts with a built-in.

    Checks commands, agents, protocols, and managers for name collisions.
    """
    for comp in manifest.components:
        if comp.type == "command":
            _check_command_name_conflict(comp.name)
        elif comp.type == "agent":
            _check_agent_name_conflict(comp.name)
        elif comp.type == "device_protocol":
            _check_protocol_name_conflict(comp.name)
        # device_manager — no built-in conflict check needed yet


# Built-in names never change during a process lifetime. The first
# install on a fresh Pi was spending several seconds re-importing every
# command module just to read its `command_name` attribute, so cache it.
_BUILTIN_COMMAND_NAMES: set[str] | None = None


def _get_builtin_command_names() -> set[str]:
    global _BUILTIN_COMMAND_NAMES
    if _BUILTIN_COMMAND_NAMES is not None:
        return _BUILTIN_COMMAND_NAMES

    names: set[str] = set()
    try:
        import commands
        import importlib
        import pkgutil
        from jarvis_command_sdk import IJarvisCommand

        for _, mod_name, _ in pkgutil.iter_modules(commands.__path__):
            try:
                module = importlib.import_module(f"commands.{mod_name}")
            except Exception:
                continue
            for attr in dir(module):
                cls = getattr(module, attr)
                if (isinstance(cls, type)
                        and issubclass(cls, IJarvisCommand)
                        and cls is not IJarvisCommand):
                    try:
                        names.add(cls().command_name)
                    except Exception:
                        continue
    except ImportError:
        pass  # Not in node context

    _BUILTIN_COMMAND_NAMES = names
    return names


_OVERRIDABLE_BUILTIN_COMMAND_NAMES: set[str] | None = None


def _get_overridable_builtin_command_names() -> set[str]:
    """Built-in command names whose class sets ``overridable = True``.

    These built-ins (e.g. ``control_device``) are explicitly designed to be
    replaced by a Pantry package. The install allows the override and disables
    the built-in post-install, so no ``use_external_devices`` toggle is needed
    first.
    """
    global _OVERRIDABLE_BUILTIN_COMMAND_NAMES
    if _OVERRIDABLE_BUILTIN_COMMAND_NAMES is not None:
        return _OVERRIDABLE_BUILTIN_COMMAND_NAMES

    names: set[str] = set()
    try:
        import commands
        import importlib
        import pkgutil
        from jarvis_command_sdk import IJarvisCommand

        for _, mod_name, _ in pkgutil.iter_modules(commands.__path__):
            try:
                module = importlib.import_module(f"commands.{mod_name}")
            except Exception:
                continue
            for attr in dir(module):
                cls = getattr(module, attr)
                if (isinstance(cls, type)
                        and issubclass(cls, IJarvisCommand)
                        and cls is not IJarvisCommand
                        and getattr(cls, "overridable", False)):
                    try:
                        names.add(cls().command_name)
                    except Exception:
                        continue
    except ImportError:
        pass  # Not in node context

    _OVERRIDABLE_BUILTIN_COMMAND_NAMES = names
    return names


def _check_command_name_conflict(command_name: str) -> None:
    """Check if the command name conflicts with a built-in command.

    Two kinds of built-in are override-eligible and must NOT block the install:
    - one explicitly marked ``overridable`` (e.g. ``control_device``) — the
      install disables it post-install so the override takes effect; and
    - one already *disabled* in the command registry (e.g. via
      ``smart_home.use_external_devices``, which publishes a ``toggle_command``).
    command_discovery_service already honors the disabled case ("allow custom
    command to override a DISABLED built-in"), so the install-time check mirrors
    it — otherwise the override package could never install to take over.
    """
    if command_name not in _get_builtin_command_names():
        return

    # A built-in explicitly designed to be replaced is always override-eligible.
    if command_name in _get_overridable_builtin_command_names():
        return

    # Only enabled built-ins block an install; a disabled one is override-eligible.
    try:
        from db import SessionLocal
        from repositories.command_registry_repository import CommandRegistryRepository

        db = SessionLocal()
        try:
            if not CommandRegistryRepository(db).get_all().get(command_name, True):
                return  # built-in disabled → allow the override package
        finally:
            db.close()
    except Exception:
        # Registry unavailable (e.g. not in node context) — fall back to the
        # conservative "enabled built-in blocks" behavior.
        pass

    raise InstallError(
        f"Command name '{command_name}' conflicts with a built-in command"
    )


def _check_agent_name_conflict(agent_name: str) -> None:
    """Check if the agent name conflicts with a built-in agent (not custom-installed)."""
    try:
        from utils.agent_discovery_service import get_agent_discovery_service
        svc = get_agent_discovery_service()
        existing = svc.get_agent(agent_name)
        if existing and not _is_custom_installed(existing, "agents", "custom_agents"):
            raise InstallError(f"Agent name '{agent_name}' conflicts with built-in agent")
    except ImportError:
        pass


def _check_protocol_name_conflict(protocol_name: str) -> None:
    """Check if the protocol name conflicts with a built-in device protocol (not custom-installed)."""
    try:
        from utils.device_family_discovery_service import get_device_family_discovery_service
        svc = get_device_family_discovery_service()
        existing = svc.get_family(protocol_name)
        if existing and not _is_custom_installed(existing, "device_families", "custom_families"):
            raise InstallError(f"Protocol name '{protocol_name}' conflicts with built-in protocol")
    except ImportError:
        pass


def _is_custom_installed(obj: object, package_dir: str, custom_subdir: str) -> bool:
    """Check if an object was loaded from a custom (Pantry-installed) directory."""
    try:
        module = type(obj).__module__ or ""
        return f"{package_dir}.{custom_subdir}" in module
    except Exception:
        return False


# Minimum free disk space before invoking apt. Apt-get install often needs
# 100-300 MB; 500 MB leaves headroom for the package + its postinst scripts
# without filling /var on a Pi with a small SD card.
_APT_MIN_FREE_BYTES = 500 * 1024 * 1024

_APT_WRAPPER_PATH = Path("/usr/local/sbin/jarvis-apt-install")
_APT_SOURCE_WRAPPER_PATH = Path("/usr/local/sbin/jarvis-apt-source")

# Matches _install_pip_deps' 300s timeout. Apt can be slow on first run
# (apt-get update + index rebuild on stale Pis), but anything over this is
# almost certainly a stuck lock or DNS issue.
_APT_TIMEOUT_SECONDS = 300

# 3rd-party apt source setup (key fetch + sources.list write) should be
# fast — it's bounded by the curl key fetch. 60s is generous; anything
# slower is a stuck DNS / GitHub Pages issue.
_APT_SOURCE_TIMEOUT_SECONDS = 60


def _install_apt_sources(manifest: CommandManifest) -> None:
    """Register 3rd-party apt sources declared in the manifest.

    Runs BEFORE _install_apt_deps so `apt install <pkg>` can resolve any
    package that lives outside the default Debian repos (e.g. raspotify).
    Idempotent — the wrapper no-ops when key+sources.list files already
    match.

    Source values reaching this point have already been validated by
    Pantry's apt-source-allowlist against an exact (name, key_url, repo)
    match (or are author-trusted for local installs); the wrapper enforces
    shape only.
    """
    apt_sources = getattr(manifest, "apt_sources", None) or []
    if not apt_sources:
        return

    for src in apt_sources:
        name = getattr(src, "name", None) or src.get("name", "") if not isinstance(src, str) else ""
        key_url = getattr(src, "key_url", None) or (src.get("key_url", "") if isinstance(src, dict) else "")
        repo = getattr(src, "repo", None) or (src.get("repo", "") if isinstance(src, dict) else "")
        if not (name and key_url and repo):
            raise InstallError(
                f"apt_sources entry missing required field(s) (name/key_url/repo): {src}"
            )

        logger.info("Registering apt source", name=name, repo=repo)
        try:
            result = subprocess.run(
                ["sudo", str(_APT_SOURCE_WRAPPER_PATH), name, key_url, repo],
                capture_output=True,
                text=True,
                timeout=_APT_SOURCE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as e:
            raise InstallError(
                f"apt-source wrapper missing at {_APT_SOURCE_WRAPPER_PATH} — "
                f"re-run install.sh to deploy it (source: {name}): {e}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise InstallError(
                f"apt-source registration timed out after {_APT_SOURCE_TIMEOUT_SECONDS}s "
                f"(source: {name})"
            ) from e

        if result.returncode != 0:
            raise InstallError(
                f"apt-source registration failed: {result.stderr.strip()} (source: {name})"
            )


def _install_apt_deps(manifest: CommandManifest) -> None:
    """Install apt dependencies declared in the manifest via the sudoers wrapper."""
    apt_packages = getattr(manifest, "apt_packages", None) or []
    if not apt_packages:
        return

    free = shutil.disk_usage("/").free
    if free < _APT_MIN_FREE_BYTES:
        raise InstallError(
            f"insufficient disk: need ≥500MB free, have {free // (1024 * 1024)}MB"
        )

    logger.info("Installing apt dependencies", packages=apt_packages)
    try:
        result = subprocess.run(
            ["sudo", str(_APT_WRAPPER_PATH), *apt_packages],
            capture_output=True,
            text=True,
            timeout=_APT_TIMEOUT_SECONDS,
        )
    except FileNotFoundError as e:
        raise InstallError(
            f"apt wrapper missing at {_APT_WRAPPER_PATH} — re-run install.sh "
            f"to deploy it (packages: {apt_packages}): {e}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise InstallError(
            f"apt install timed out after {_APT_TIMEOUT_SECONDS}s "
            f"(packages: {apt_packages})"
        ) from e

    if result.returncode != 0:
        raise InstallError(
            f"apt install failed: {result.stderr.strip()} (packages: {apt_packages})"
        )


# ── post_install ops ────────────────────────────────────────────────────
#
# Declarative ops the node runs after pip+apt to make a freshly-installed
# apt package usable by the package — currently just `shairport-sync`'s pa
# backend + systemd User=pi drop-in. Each op is bounded by a Pantry-side
# allow-list checked at submission time; the sudoers-gated wrapper at
# /usr/local/sbin/jarvis-post-install does the privileged write.
#
# Idempotent: re-running install on the same package re-applies the same
# rendered drop-in (no-op when content matches) and the same config-file
# upsert (no-op when value matches). Failures raise InstallError, aborting
# the install in the same way an apt or pip failure would.

_POST_INSTALL_WRAPPER_PATH = Path("/usr/local/sbin/jarvis-post-install")

# Matches the apt/pip 300s timeout pattern.
_POST_INSTALL_TIMEOUT_SECONDS = 300


def _jarvis_user_and_uid() -> tuple[str, int]:
    """Return (User=, UID) of the jarvis-node systemd unit on this host.

    We infer rather than hardcode so multi-user nodes or future containers
    where the service user isn't `pi` don't accidentally get drop-ins
    pinned to the wrong account. Falls back to the current process's user
    if systemctl isn't available (dev environments).
    """
    try:
        result = subprocess.run(
            ["systemctl", "show", "jarvis-node", "--property=User", "--value"],
            capture_output=True, text=True, timeout=5.0,
        )
        username = (result.stdout or "").strip() if result.returncode == 0 else ""
    except (FileNotFoundError, subprocess.TimeoutExpired):
        username = ""

    if not username:
        # No systemctl (likely a Mac dev box). Use the running user — won't
        # be invoked on real systems anyway, but keeps tests / smoke runs
        # from blowing up.
        import getpass
        username = getpass.getuser()

    try:
        import pwd
        uid = pwd.getpwnam(username).pw_uid
    except (KeyError, ImportError):
        uid = os.geteuid()
    return username, uid


def _home_for_user(username: str) -> str:
    """Best-effort home directory for the node user.

    Falls back to /home/<username> so install never aborts on a misconfigured
    passwd entry — the conventional layout works for every supported node
    setup (pi, jarvis_user, container users).
    """
    try:
        import pwd
        return pwd.getpwnam(username).pw_dir
    except (KeyError, ImportError):
        return f"/home/{username}"


def _expand_post_install_tokens(value: Any, uid: int, username: str) -> Any:
    """Recursively substitute ${UID}/${USER}/${HOME} tokens inside strings.

    Op payloads are JSON-shaped, so we only need to walk dicts, lists, and
    strings. Other primitives pass through unchanged.

    Why not rely on systemd's own %U/%h specifiers? Because %h resolves to
    the *manager's* home (i.e. /root for system-mode systemd), not the
    User=user's home — easy to get wrong, and we hit it with the audacy
    package's MPDCONF env value. Install-time substitution makes the
    resulting drop-in self-contained and not dependent on a specifier
    we can't override.
    """
    if isinstance(value, str):
        home = _home_for_user(username)
        return (
            value
            .replace("${UID}", str(uid))
            .replace("${USER}", username)
            .replace("${HOME}", home)
        )
    if isinstance(value, list):
        return [_expand_post_install_tokens(v, uid, username) for v in value]
    if isinstance(value, dict):
        return {k: _expand_post_install_tokens(v, uid, username) for k, v in value.items()}
    return value


def _run_post_install(manifest: CommandManifest, package_name: str) -> None:
    """Execute manifest.post_install ops via the sudoers-gated wrapper.

    Each op is invoked as:
        sudo /usr/local/sbin/jarvis-post-install --package <name> <op-subcmd>
    with the op's JSON payload on stdin. The wrapper handles the actual
    privileged write (systemd drop-in or config-file upsert) and restarts
    affected services as needed.

    Raises InstallError on any op failure — matches the apt/pip path's
    abort-on-failure semantics.
    """
    ops = getattr(manifest, "post_install", None) or []
    if not ops:
        return

    if not _POST_INSTALL_WRAPPER_PATH.exists():
        raise InstallError(
            f"post-install wrapper missing at {_POST_INSTALL_WRAPPER_PATH} — "
            f"re-run install.sh to deploy it (ops requested: "
            f"{[op.get('type') for op in ops]})"
        )

    jarvis_user, jarvis_uid = _jarvis_user_and_uid()
    logger.info(
        "Running post_install ops",
        package=package_name,
        op_count=len(ops),
        jarvis_user=jarvis_user,
        jarvis_uid=jarvis_uid,
    )

    # Map op type → wrapper subcommand. Keep names in lockstep with the
    # SDK schema and Pantry allow-list — drift here means submissions pass
    # Pantry but fail at install.
    _OP_SUBCOMMAND = {
        "configure_systemd_service": "configure-systemd-service",
        "set_config_file_value": "set-config-file-value",
    }

    for i, raw_op in enumerate(ops):
        op_type = raw_op.get("type")
        subcmd = _OP_SUBCOMMAND.get(op_type or "")
        if subcmd is None:
            raise InstallError(
                f"post_install[{i}]: unknown op type {op_type!r}. "
                f"Known: {sorted(_OP_SUBCOMMAND.keys())}"
            )

        # Resolve `run_as: jarvis_user` sentinel and ${UID} tokens.
        payload = {k: v for k, v in raw_op.items() if k != "type"}
        if payload.get("run_as") == "jarvis_user":
            payload["run_as"] = jarvis_user
        payload = _expand_post_install_tokens(payload, jarvis_uid, jarvis_user)

        logger.info(
            "Dispatching post_install op",
            package=package_name,
            op_index=i,
            op_type=op_type,
            target=payload.get("service") or payload.get("file"),
        )

        try:
            result = subprocess.run(
                ["sudo", str(_POST_INSTALL_WRAPPER_PATH),
                 "--package", package_name, subcmd],
                input=json.dumps(payload),
                capture_output=True,
                text=True,
                timeout=_POST_INSTALL_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as e:
            raise InstallError(
                f"post_install[{i}]: wrapper not invokable: {e}"
            ) from e
        except subprocess.TimeoutExpired as e:
            raise InstallError(
                f"post_install[{i}]: timed out after "
                f"{_POST_INSTALL_TIMEOUT_SECONDS}s (op={op_type})"
            ) from e

        if result.returncode != 0:
            raise InstallError(
                f"post_install[{i}] ({op_type}) failed: "
                f"{(result.stderr or result.stdout).strip()[:500]}"
            )

        logger.info(
            "post_install op succeeded",
            package=package_name,
            op_index=i,
            op_type=op_type,
            output=(result.stdout or "").strip()[:200],
        )


def _remove_post_install_dropins(package_name: str) -> None:
    """Best-effort: invoke the sudoers-gated wrapper to remove drop-ins
    this package previously wrote (matched by managed-by marker).

    Config-file edits (set_config_file_value) are NOT auto-reverted —
    no straightforward way to do that without recording originals, and
    leaving an apt-package's config file as the user wants it is the
    safer default. Operators can hand-revert from .bak files if needed.

    Never raises — uninstall must succeed even if the wrapper isn't
    installed or systemctl is unavailable.
    """
    if not _POST_INSTALL_WRAPPER_PATH.exists():
        return
    try:
        result = subprocess.run(
            ["sudo", str(_POST_INSTALL_WRAPPER_PATH),
             "--package", package_name, "remove-managed-dropins"],
            capture_output=True, text=True, timeout=30.0,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("post_install dropin removal skipped", error=str(e))
        return
    if result.returncode != 0:
        logger.warning(
            "post_install dropin removal failed",
            package=package_name,
            stderr=(result.stderr or "").strip()[:300],
        )
        return
    out = (result.stdout or "").strip()
    if out:
        logger.info("post_install dropins removed", package=package_name, detail=out)


def _snapshot_installed_dists() -> set[tuple[str, str]]:
    """Snapshot every installed dist as (name, version) tuples.

    Used around `_install_pip_deps` to detect whether pip actually changed
    anything. importlib.metadata reads from .dist-info/ on disk, so the
    second call sees newly written dist-infos even though the long-running
    process hasn't imported them yet.
    """
    try:
        from importlib.metadata import distributions
        return {
            ((d.metadata["Name"] or "").lower(), d.version or "")
            for d in distributions()
        }
    except Exception:
        return set()


def _install_pip_deps(manifest: CommandManifest, *, state: dict | None = None) -> None:
    """Install pip dependencies declared in the manifest.

    When ``state`` is provided, the names of packages that pip actually
    installed or upgraded are recorded at ``state["pip_deps_installed"]``.
    The MQTT install handler uses this to schedule a service restart —
    newly installed top-level imports won't be picked up by the long-running
    Python process otherwise (a failed ``ImportError`` at boot caches
    ``None`` for the import target).

    We diff installed-dist snapshots before/after rather than trusting the
    manifest list, so a reinstall where every dep is already at the
    requested version skips the restart entirely.
    """
    deps: list[str] = []

    # From manifest packages
    for pkg in manifest.packages:
        if pkg.version:
            if pkg.version[0].isdigit():
                deps.append(f"{pkg.name}=={pkg.version}")
            else:
                deps.append(f"{pkg.name}{pkg.version}")
        else:
            deps.append(pkg.name)

    if not deps:
        return

    logger.info("Installing pip dependencies", packages=deps)
    before = _snapshot_installed_dists()
    # --prefer-binary: grab pre-built wheels instead of compiling C
    # extensions from source (lxml, etc.). ARM64 wheels exist for most
    # packages and avoid the 100% CPU + swap thrashing that kills the
    # node service on Pi Zero 2 W (512 MB).
    # nice -n 15: lower priority so the install doesn't starve the voice
    # pipeline, MQTT, SSH, and other threads sharing the same 4 cores.
    #
    # Timeout bumped 300s → 600s in v0.1.67: music-assistant-client has
    # ~10+ transitive deps (zeroconf, mashumaro, music_assistant_models,
    # etc.) and on Pi Zero the 5-minute cap was just barely too tight,
    # causing silent partial installs (the May-2026 beta saw the kitchen
    # Pi end up with shairport-sync installed via apt but no Python
    # client in the venv, so every music command failed with
    # `music-assistant-client is not installed`). Tunable per-node via
    # the ``pantry_pip_install_timeout_secs`` config key.
    timeout_secs = 600
    try:
        from utils.config_service import Config
        timeout_secs = max(60, Config.get_int("pantry_pip_install_timeout_secs", 600))
    except Exception:
        # Config service not available during early-install paths;
        # fall back to the hardcoded default.
        pass
    result = subprocess.run(
        ["nice", "-n", "15", sys.executable, "-m", "pip", "install",
         "--quiet", "--prefer-binary"] + deps,
        capture_output=True,
        text=True,
        timeout=timeout_secs,
    )
    if result.returncode != 0:
        raise InstallError(f"pip install failed: {result.stderr.strip()}")

    if state is not None:
        after = _snapshot_installed_dists()
        changed = sorted({name for (name, _ver) in (after - before)})
        if changed:
            state["pip_deps_installed"] = changed
            logger.info("pip install changed dists", changed=changed)
        else:
            # All declared deps were already at the requested version —
            # skip the restart and the ~30-60s of post-restart boot.
            logger.info(
                "pip install completed with no dist changes — skipping restart",
                declared=deps,
            )


def _check_jarvis_dependencies(deps: list[str]) -> None:
    """Verify all declared Jarvis package dependencies are installed.

    Each dependency must have metadata at ``~/.jarvis/packages/<dep>.json``,
    meaning the package was previously installed via the store.
    """
    for dep in deps:
        meta_path = PACKAGES_DIR / f"{dep}.json"
        if not meta_path.exists():
            raise InstallError(
                f"Missing Jarvis dependency: '{dep}' is not installed. "
                f"Install it first, then retry."
            )


# Map component type → (SDK base class name, entry point filename)
_COMPONENT_BASE_CLASSES: dict[str, str] = {
    "command": "IJarvisCommand",
    "agent": "IJarvisAgent",
    "device_protocol": "IJarvisDeviceProtocol",
    "device_manager": "IJarvisDeviceManager",
    "prompt_provider": "IJarvisPromptProvider",
}


def _find_main_class_name_ast(entry_point_path: Path, base_class_name: str) -> str | None:
    """Parse an entry point file and find the class inheriting from *base_class_name*.

    Uses AST only — no runtime imports needed.
    """
    import ast

    try:
        source = entry_point_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except Exception:
        return None

    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            name: str | None = None
            if isinstance(base, ast.Name):
                name = base.id
            elif isinstance(base, ast.Attribute):
                name = base.attr
            if name and base_class_name in name:
                return node.name
    return None


def _generate_package_namespace(
    package_name: str,
    manifest: CommandManifest,
) -> None:
    """Generate ``~/.jarvis/packages/<name>/__init__.py`` that re-exports
    the main component classes so other packages can import them.

    Example output for a device_protocol named ``nest``::

        # Auto-generated by Jarvis — do not edit
        from device_families.custom_families.nest.protocol import NestProtocol
        __all__ = ["NestProtocol"]
    """
    from core.command_manifest import COMPONENT_ENTRY_POINTS

    imports: list[str] = []
    all_names: list[str] = []

    for comp in manifest.components:
        if comp.type == "routine":
            continue

        base_class_name = _COMPONENT_BASE_CLASSES.get(comp.type)
        if not base_class_name:
            continue

        # Determine install directory and entry point filename
        install_rel = COMPONENT_INSTALL_DIRS.get(comp.type)
        if not install_rel:
            continue

        entry_filename = COMPONENT_ENTRY_POINTS.get(comp.type, "command.py")
        entry_point = _PROJECT_DIR / install_rel / comp.name / entry_filename

        if not entry_point.exists():
            logger.debug("Namespace skip: entry point not found", component=comp.name, path=str(entry_point))
            continue

        class_name = _find_main_class_name_ast(entry_point, base_class_name)
        if not class_name:
            logger.debug("Namespace skip: no class found", component=comp.name, base=base_class_name)
            continue

        # Build Python import path: e.g. device_families.custom_families.nest.protocol
        module_stem = entry_point.stem  # "protocol", "command", etc.
        import_path = f"{install_rel.replace('/', '.')}.{comp.name}.{module_stem}"

        imports.append(f"from {import_path} import {class_name}")
        all_names.append(class_name)

    if not imports:
        return

    ns_dir = PACKAGES_DIR / package_name
    ns_dir.mkdir(parents=True, exist_ok=True)

    lines = ["# Auto-generated by Jarvis — do not edit"]
    lines.extend(imports)
    lines.append(f"__all__ = {all_names!r}")
    lines.append("")  # trailing newline

    init_path = ns_dir / "__init__.py"
    init_path.write_text("\n".join(lines), encoding="utf-8")
    logger.info("Generated package namespace", package=package_name, exports=all_names)


def _write_store_metadata(
    install_dir: Path,
    manifest: CommandManifest,
    repo_url: str,
    danger_rating: int | None = None,
    verified: bool = False,
) -> None:
    """Write .store_metadata.json alongside the installed command."""
    metadata = {
        "command_name": manifest.name,
        "version": manifest.version,
        "repo_url": repo_url,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "danger_rating": danger_rating,
        "verified": verified,
        "display_name": manifest.display_name,
        "author": manifest.author.github if manifest.author else None,
    }
    with open(install_dir / STORE_METADATA_FILE, "w") as f:
        json.dump(metadata, f, indent=2)


def _collect_shared_dirs(repo_dir: Path, component_paths: list[str]) -> list[Path]:
    """Find directories in the repo that aren't component dirs and contain .py files.

    These are shared code directories (services/, utils/, helpers/, etc.) that
    components may import from.

    Args:
        repo_dir: Cloned repo root.
        component_paths: List of component entry point paths (relative to repo).

    Returns:
        List of shared directory paths (absolute).
    """
    # Collect all top-level parent dirs that contain component entry points
    component_top_dirs: set[str] = set()
    for cp in component_paths:
        parts = Path(cp).parts
        if len(parts) > 1:
            component_top_dirs.add(parts[0])

    shared: list[Path] = []
    skip = {".git", ".venv", "__pycache__", ".pytest_cache", "node_modules"}

    for entry in repo_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name in skip or entry.name.startswith("."):
            continue
        # If this top-level dir is not a component parent dir and has .py files, it's shared
        if entry.name not in component_top_dirs and any(entry.rglob("*.py")):
            shared.append(entry)

    return shared


def _install_shared_code(
    package_name: str,
    shared_dirs: list[Path],
    repo_dir: Path,
) -> Path:
    """Install shared code to a package-specific lib directory.

    Shared code lives at ~/.jarvis/packages/<name>/<name>_lib/ and is added
    to sys.path at node startup so all scattered components can import from
    it. The package-name prefix on the lib dir (vs a bare "lib") makes the
    namespace distinct per package, which matters for diagnostics and for
    any tooling that walks ~/.jarvis/packages.

    Args:
        package_name: The package name.
        shared_dirs: Shared directories from the repo to install.
        repo_dir: Cloned repo root (for copying root-level .py files).

    Returns:
        The shared lib directory.
    """
    lib_dir = PACKAGES_DIR / package_name / f"{package_name}_lib"
    if lib_dir.exists():
        _force_rmtree(lib_dir)
    lib_dir.mkdir(parents=True, exist_ok=True)

    # Copy shared directories (services/, utils/, etc.)
    for shared_dir in shared_dirs:
        dest = lib_dir / shared_dir.name
        shutil.copytree(shared_dir, dest, ignore=shutil.ignore_patterns(
            "__pycache__", "*.pyc", ".pytest_cache",
        ))
        # Ensure each subdir is a package
        init_file = dest / "__init__.py"
        if not init_file.exists():
            init_file.write_text("")

    # Copy root-level .py helper files (not manifests)
    for py_file in repo_dir.glob("*.py"):
        shutil.copy2(py_file, lib_dir / py_file.name)

    logger.info("Installed shared code", package=package_name, dir=str(lib_dir))
    return lib_dir


def _force_rmtree(path: Path) -> None:
    """Remove a directory tree, falling back to sudo for root-owned leftovers.

    Same-version reinstalls and updates hit this whenever the previous install
    left root-owned files behind — most commonly __pycache__ entries written
    by a process that briefly ran as root, or files dropped by a post-install
    op. shutil.rmtree on those raises PermissionError; sudo -n rm -rf is the
    only way out without manual SSH intervention. If sudo NOPASSWD isn't
    configured we raise an InstallError carrying the exact command an operator
    can run to unblock — better than a bare 'Permission denied: PosixPath(...)'.
    """
    try:
        shutil.rmtree(path)
        return
    except PermissionError as e:
        logger.warning(
            "rmtree blocked by permissions; attempting sudo fallback",
            path=str(path), error=str(e),
        )
    except FileNotFoundError:
        return

    try:
        subprocess.run(
            ["sudo", "-n", "rm", "-rf", str(path)],
            check=True,
            capture_output=True,
        )
        return
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        stderr = e.stderr.decode("utf-8", errors="replace") if hasattr(e, "stderr") and e.stderr else ""
        raise InstallError(
            f"Cannot replace existing install at {path}: permission denied "
            f"and 'sudo -n rm -rf' fallback also failed ({stderr.strip() or e}). "
            f"Run `sudo rm -rf {path}` on the node and retry the install."
        )


def _install_component(repo_dir: Path, comp_type: str, comp_name: str, comp_path: str) -> Path:
    """Install a single component to its target directory.

    Args:
        repo_dir: Cloned repo root.
        comp_type: Component type (command, agent, device_protocol, device_manager).
        comp_name: Component name.
        comp_path: Path to the component's main file relative to repo root.

    Returns:
        The install directory for this component.
    """
    base_dir = COMPONENT_INSTALL_DIRS.get(comp_type)
    if not base_dir:
        raise InstallError(f"Unknown component type: {comp_type}")

    install_dir = _PROJECT_DIR / base_dir / comp_name
    if install_dir.exists():
        logger.warning("Replacing existing component", type=comp_type, name=comp_name)
        _force_rmtree(install_dir)

    install_dir.mkdir(parents=True, exist_ok=True)

    # Copy the component file
    source_file = repo_dir / comp_path
    shutil.copy2(source_file, install_dir / source_file.name)

    # Copy sibling .py files from the same directory
    source_dir = source_file.parent
    for py_file in source_dir.glob("*.py"):
        if py_file.name != source_file.name:
            shutil.copy2(py_file, install_dir / py_file.name)

    # Write __init__.py
    (install_dir / "__init__.py").write_text(
        f"# Custom {comp_type}: {comp_name}\n"
    )

    logger.info("Installed component", type=comp_type, name=comp_name, dir=str(install_dir))
    return install_dir


def _write_package_metadata(
    manifest: CommandManifest,
    repo_url: str,
    component_dirs: dict[str, str],
    danger_rating: int | None = None,
    verified: bool = False,
    previous_version: str | None = None,
) -> None:
    """Write package metadata to ~/.jarvis/packages/<name>.json.

    This tracks all component install dirs for clean uninstall.
    """
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    # Persist pip-package declarations to the metadata file so the
    # startup self-heal (verify_pantry_pip_deps) can verify that the
    # declared deps are actually present in the venv on every boot.
    # Without this, the May-2026 beta-blocker case repeats: a Pantry
    # install whose pip step silently fails leaves the package's
    # metadata file claiming "installed" while the dep is missing,
    # and the user only learns about it when they try to use the
    # command at runtime ("music-assistant-client is not installed").
    pip_packages = [
        {"name": pkg.name, "version": pkg.version}
        for pkg in manifest.packages
    ]
    metadata = {
        "package_name": manifest.name,
        "package_type": manifest.package_type,
        "version": manifest.version,
        "repo_url": repo_url,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "danger_rating": danger_rating,
        "verified": verified,
        "display_name": manifest.display_name,
        "author": manifest.author.github if manifest.author else None,
        "components": [
            {"type": c.type, "name": c.name, "path": c.path}
            for c in manifest.components
        ],
        "component_dirs": component_dirs,
        "jarvis_dependencies": list(manifest.jarvis_dependencies),
        "pip_packages": pip_packages,
    }
    if previous_version is not None:
        # Set when this install replaced an existing version and a
        # .previous rollback snapshot was written — mobile uses it for the
        # "Revert to {previous_version}" action.
        metadata["previous_version"] = previous_version
    meta_path = PACKAGES_DIR / f"{manifest.name}.json"
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)


def _write_previous_snapshot(package_name: str) -> str | None:
    """Snapshot the live install to ``~/.jarvis/packages/<name>/.previous/``.

    Called from ``_do_install`` after the pre-flight gate passes, only when
    UPDATING an existing package. The snapshot is the rollback source of
    truth — it captures:

    - ``meta.json``: the old package metadata json
    - ``__init__.py``: the old auto-generated namespace (if any)
    - ``<name>_lib/`` (or legacy ``lib/``): the old shared-code dir
    - ``components/<type>__<comp>/``: a copy of every live dir recorded in
      the old metadata's ``component_dirs`` (":" isn't filesystem-safe, so
      the key separator becomes "__")

    Any prior ``.previous`` is replaced wholesale (we keep N-1, not N-k).

    Atomicity invariant: ``.previous/meta.json`` exists only when the
    snapshot is complete, and a prior good ``.previous`` survives until the
    new one is fully built. The snapshot is assembled in ``.previous.tmp``
    with ``meta.json`` copied LAST, then swapped into place — a mid-copy
    failure removes the tmp dir and re-raises, so ``_do_install``'s
    best-effort warning correctly means "no rollback available" (never "a
    half-snapshot revert could silently restore from").

    Returns:
        The old version string, or None when there was no metadata to
        snapshot.
    """
    meta_path = PACKAGES_DIR / f"{package_name}.json"
    if not meta_path.exists():
        return None
    with open(meta_path) as f:
        old_meta = json.load(f)

    pkg_dir = PACKAGES_DIR / package_name
    prev_dir = pkg_dir / ".previous"
    tmp_dir = pkg_dir / ".previous.tmp"
    if tmp_dir.exists():
        _force_rmtree(tmp_dir)

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)

        ns_init = pkg_dir / "__init__.py"
        if ns_init.exists():
            shutil.copy2(ns_init, tmp_dir / "__init__.py")

        # Both shared-code conventions: <name>_lib (current) and lib (legacy).
        for lib_name in (f"{package_name}_lib", "lib"):
            lib_dir = pkg_dir / lib_name
            if lib_dir.is_dir():
                shutil.copytree(
                    lib_dir,
                    tmp_dir / lib_name,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
                )

        components_dir = tmp_dir / "components"
        for comp_key, comp_dir_str in (old_meta.get("component_dirs") or {}).items():
            comp_dir = Path(comp_dir_str)
            if not comp_dir.is_dir():
                continue
            shutil.copytree(
                comp_dir,
                components_dir / comp_key.replace(":", "__"),
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )

        # meta.json LAST — its presence is the "snapshot is complete" marker.
        shutil.copy2(meta_path, tmp_dir / "meta.json")
    except BaseException:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise

    # Snapshot fully built — only now retire the prior one and swap in.
    _force_rmtree(prev_dir)
    os.replace(tmp_dir, prev_dir)

    old_version = old_meta.get("version")
    logger.info(
        "Wrote .previous rollback snapshot",
        package=package_name,
        version=old_version,
    )
    return old_version


def has_previous_version(package_name: str) -> bool:
    """True when a usable ``.previous`` rollback snapshot exists."""
    return (PACKAGES_DIR / package_name / ".previous" / "meta.json").exists()


# Pre-flight validation runs `command_store.py validate` in a SUBPROCESS so
# component imports execute against the node's real venv (fresh interpreter,
# real SDK) without polluting this long-running process's module state.
# 180s bounds a Pi Zero's worst-case import-test pass.
_PREFLIGHT_VALIDATE_TIMEOUT_SECONDS = 180


def _run_preflight_validation(repo_dir: Path) -> None:
    """Import-test the downloaded repo in a subprocess BEFORE any scatter.

    Raises InstallError on validation failure. Nothing has been written to
    the live component dirs at this point; the apt/pip deps installed just
    before this gate are idempotent and harmless to keep on failure.
    """
    try:
        result = subprocess.run(
            [sys.executable, str(_PROJECT_DIR / "scripts" / "command_store.py"),
             "validate", str(repo_dir)],
            cwd=_PROJECT_DIR,
            capture_output=True,
            text=True,
            timeout=_PREFLIGHT_VALIDATE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as e:
        raise InstallError(
            f"Package failed validation: timed out after "
            f"{_PREFLIGHT_VALIDATE_TIMEOUT_SECONDS}s"
        ) from e

    if result.returncode == 0:
        return

    # Surface the most specific line: a per-component "FAIL" line when
    # present (stdout for import failures, stderr for manifest failures),
    # otherwise the last stderr line.
    detail = ""
    for line in (result.stdout or "").splitlines() + (result.stderr or "").splitlines():
        if "FAIL" in line:
            detail = line.strip()
            break
    if not detail:
        stderr_lines = [ln.strip() for ln in (result.stderr or "").splitlines() if ln.strip()]
        detail = stderr_lines[-1] if stderr_lines else f"exit code {result.returncode}"
    raise InstallError(f"Package failed validation: {detail}")


def _do_install(
    repo_dir: Path,
    source_label: str,
    *,
    skip_tests: bool = False,
    state: dict | None = None,
) -> CommandManifest:
    """Core install logic — validates, installs deps, gates, then scatters.

    Order matters: version floors run before any disk writes; apt + pip deps
    install before the pre-flight validate gate (so the gate's import tests
    can see them — both are idempotent); nothing is scattered into the live
    component dirs until the gate passes.

    Args:
        repo_dir: Path to repo directory (cloned or local).
        source_label: Display label for logging (URL or path).
        skip_tests: Skip the pre-flight validation gate (dev/--local escape
            hatch; the MQTT install path never sets it).
        state: Optional mutable dict the installer fills with side-info
            useful to the caller (e.g. ``pip_deps_installed``).

    Returns:
        The installed manifest.
    """
    # 1. Validate structure + manifest
    manifest = _validate_repo_structure(repo_dir)

    # 1b. Enforce version floors BEFORE any disk writes
    _enforce_version_floors(manifest)

    # 2. Check platform
    _check_platform_compatibility(manifest)

    # 3. Check Jarvis package dependencies
    if manifest.jarvis_dependencies:
        _check_jarvis_dependencies(manifest.jarvis_dependencies)

    # 4. Check name conflicts (skip if package already installed — it's an update)
    already_installed: bool = (PACKAGES_DIR / f"{manifest.name}.json").exists()
    if already_installed:
        logger.info("Updating existing package", package=manifest.name)
    else:
        _check_name_conflicts(manifest)

    # 5a. Register 3rd-party apt sources (before _install_apt_deps so the
    # next apt-get install can resolve packages from them).
    _install_apt_sources(manifest)

    # 5b. Install apt deps (before pip so a fast apt failure aborts cleanly).
    _install_apt_deps(manifest)

    # 5c. Install pip deps — before the pre-flight gate so the gate's import
    # tests run against the declared dependencies. Idempotent: a reinstall
    # where every dep is already at the requested version is a no-op, and
    # deps left behind by a gate failure are harmless.
    _install_pip_deps(manifest, state=state)

    # 6. Pre-flight validate gate: import-test every component in a
    # subprocess against the staged repo dir. Fails the install before
    # anything is scattered into the live directories.
    if skip_tests:
        logger.warning(
            "Skipping pre-flight validation (skip_tests)", package=manifest.name
        )
    else:
        _run_preflight_validation(repo_dir)

    # 6b. Keep N-1: when updating, snapshot the live install to .previous
    # BEFORE anything is scattered, so a failed update can be rolled back
    # (automatically at the post-restart health check, or manually from the
    # mobile package card). Best-effort — a snapshot failure must not abort
    # an install that already passed the gate; it just means rollback won't
    # be available for this update.
    previous_version: str | None = None
    if already_installed:
        try:
            previous_version = _write_previous_snapshot(manifest.name)
        except Exception as e:
            logger.warning(
                "Could not write .previous rollback snapshot",
                package=manifest.name,
                error=str(e),
            )

    # 7. Install shared code for bundles (ha_shared/, etc.)
    component_paths = [c.path for c in manifest.components]
    shared_dirs = _collect_shared_dirs(repo_dir, component_paths)
    if shared_dirs:
        _install_shared_code(manifest.name, shared_dirs, repo_dir)

    # 8. Install components
    component_dirs: dict[str, str] = {}
    for comp in manifest.components:
        install_dir = _install_component(repo_dir, comp.type, comp.name, comp.path)
        # Use type:name as key to avoid collisions (e.g., agent and manager both named "home_assistant")
        component_dirs[f"{comp.type}:{comp.name}"] = str(install_dir)

    # 9. Write package metadata (for clean uninstall + revert availability)
    _write_package_metadata(
        manifest, source_label, component_dirs, previous_version=previous_version
    )

    # 10. Also write .store_metadata.json in the first command dir
    command_comps = [c for c in manifest.components if c.type == "command"]
    if command_comps:
        first_cmd_dir = _PROJECT_DIR / COMPONENT_INSTALL_DIRS["command"] / command_comps[0].name
        if first_cmd_dir.exists():
            _write_store_metadata(first_cmd_dir, manifest, source_label)

    # 10b. Run declarative post-install ops via the sudoers-gated wrapper.
    # Comes after apt + pip so the services being configured (and any
    # config files we touch) exist on disk by the time we get here.
    _run_post_install(manifest, manifest.name)

    # 11. Generate package namespace for dependency imports
    _generate_package_namespace(manifest.name, manifest)

    # 12. Seed secrets
    _seed_secrets(manifest)

    # 13. Enable commands in registry — including one that shadows an
    # *overridable* built-in. Discovery gives the custom command precedence
    # over an overridable built-in unconditionally, so the shared registry
    # name must stay ENABLED for the override to be advertised. (Disabling it
    # here — the v0.1.145 approach — both hid the name from advertised tools
    # and got clobbered by CC command_registry config pushes, which don't
    # know about node-local overrides.)
    for comp in manifest.components:
        if comp.type == "command":
            _enable_in_registry(comp.name)

    logger.info(
        "Package installed successfully",
        package=manifest.name,
        version=manifest.version,
        components=len(manifest.components),
    )
    return manifest


def install_from_github(
    repo_url: str,
    version_tag: str | None = None,
    skip_tests: bool = False,
    *,
    state: dict | None = None,
) -> CommandManifest:
    """Install a command or bundle from a GitHub repo URL.

    Args:
        repo_url: GitHub HTTPS URL.
        version_tag: Optional git tag to checkout.
        skip_tests: Skip the pre-flight validation gate (user accepts risk).
        state: Optional dict the installer fills with side-info (e.g.
            ``pip_deps_installed``). Used by the MQTT install handler to
            decide whether the node needs a restart.

    Returns:
        The installed command's manifest.
    """
    logger.info("Installing from GitHub", repo_url=repo_url, tag=version_tag)
    repo_dir = _clone_repo(repo_url, version_tag)
    try:
        return _do_install(repo_dir, repo_url, skip_tests=skip_tests, state=state)
    finally:
        shutil.rmtree(repo_dir.parent, ignore_errors=True)


def install_from_local(
    local_path: str | Path,
    *,
    skip_tests: bool = False,
    state: dict | None = None,
) -> CommandManifest:
    """Install a command or bundle from a local directory.

    Useful for development and testing. Does not clone — reads directly
    from the given path.

    Args:
        local_path: Path to a directory containing a manifest + components.
        skip_tests: Skip the pre-flight validation gate (user accepts risk).
        state: Optional dict the installer fills with side-info (see
            ``install_from_github``).

    Returns:
        The installed command's manifest.
    """
    repo_dir = Path(local_path).resolve()
    if not repo_dir.is_dir():
        raise InstallError(f"Not a directory: {repo_dir}")
    logger.info("Installing from local path", path=str(repo_dir))
    return _do_install(repo_dir, f"local:{repo_dir}", skip_tests=skip_tests, state=state)


def validate_package(local_path: str | Path) -> dict[str, Any]:
    """Validate a package without installing it.

    Checks manifest, component paths, and tries to import each command class.
    Skips platform checks so packages can be validated on any OS.

    Args:
        local_path: Path to the package directory.

    Returns:
        Dict with 'manifest' and 'imports' results.
    """
    import importlib.util

    repo_dir = Path(local_path).resolve()
    if not repo_dir.is_dir():
        raise InstallError(f"Not a directory: {repo_dir}")

    # Validate structure + manifest (reuses existing logic)
    manifest = _validate_repo_structure(repo_dir)

    # Register installed package lib paths first so components that import
    # a jarvis_dependencies namespace (e.g. ``from nest import NestProtocol``)
    # resolve against the live install — required when this runs as the
    # pre-flight gate's subprocess against a staged repo dir.
    register_package_lib_paths()

    # sys.path additions for the import tests: repo root, legacy lib/, and
    # every *_lib / *_shared directory at the repo root (the shared-code
    # conventions — see _install_shared_code and the *_shared/ pattern).
    paths_to_add = [str(repo_dir)]
    lib_dir = repo_dir / "lib"
    if lib_dir.is_dir():
        paths_to_add.append(str(lib_dir))
    for entry in sorted(repo_dir.iterdir()):
        if entry.is_dir() and (entry.name.endswith("_lib") or entry.name.endswith("_shared")):
            paths_to_add.append(str(entry))

    # Map component types to their SDK base classes
    _TYPE_TO_BASE: dict[str, tuple[str, str]] = {
        "command": ("IJarvisCommand", "command_name"),
        "agent": ("IJarvisAgent", "name"),
        "device_protocol": ("IJarvisDeviceProtocol", "protocol_name"),
        "device_manager": ("IJarvisDeviceManager", "name"),
    }

    # Try importing each component
    imports: dict[str, dict[str, Any]] = {}
    for comp in manifest.components:
        # Routine components: validate JSON structure (no Python import)
        if comp.type == "routine":
            comp_file = repo_dir / comp.path
            if not comp_file.exists():
                imports[comp.name] = {"ok": False, "error": f"File not found: {comp.path}"}
                continue
            try:
                with open(comp_file) as f:
                    routine_data = json.load(f)
                errors: list[str] = []
                if not routine_data.get("trigger_phrases"):
                    errors.append("missing trigger_phrases")
                if not routine_data.get("steps"):
                    errors.append("missing steps")
                if not routine_data.get("response_instruction"):
                    errors.append("missing response_instruction")
                for i, step in enumerate(routine_data.get("steps", [])):
                    if not step.get("command"):
                        errors.append(f"step {i+1} missing command")
                if errors:
                    imports[comp.name] = {"ok": False, "error": f"Invalid routine: {', '.join(errors)}"}
                else:
                    step_count = len(routine_data.get("steps", []))
                    phrase_count = len(routine_data.get("trigger_phrases", []))
                    imports[comp.name] = {
                        "ok": True,
                        "class_name": f"routine ({step_count} steps, {phrase_count} triggers)",
                    }
            except json.JSONDecodeError as e:
                imports[comp.name] = {"ok": False, "error": f"Invalid JSON: {e}"}
            continue

        base_info = _TYPE_TO_BASE.get(comp.type)
        if base_info is None:
            imports[comp.name] = {"ok": True, "class_name": f"({comp.type}, no import test)"}
            continue

        base_class_name, name_property = base_info

        comp_file = repo_dir / comp.path
        if not comp_file.exists():
            imports[comp.name] = {"ok": False, "error": f"File not found: {comp.path}"}
            continue

        old_path = sys.path[:]
        try:
            for p in paths_to_add:
                if p not in sys.path:
                    sys.path.insert(0, p)

            spec = importlib.util.spec_from_file_location(
                f"_validate_{comp.type}_{comp.name}", str(comp_file)
            )
            if spec is None or spec.loader is None:
                imports[comp.name] = {"ok": False, "error": "Could not create import spec"}
                continue

            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)

            # Find the matching SDK base class subclass
            import jarvis_command_sdk
            base_class = getattr(jarvis_command_sdk, base_class_name, None)
            if base_class is None:
                imports[comp.name] = {"ok": False, "error": f"SDK class {base_class_name} not found"}
                continue

            found_class = None
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if (
                    isinstance(attr, type)
                    and issubclass(attr, base_class)
                    and attr is not base_class
                ):
                    found_class = attr
                    break

            if found_class is None:
                imports[comp.name] = {"ok": False, "error": f"No {base_class_name} subclass found"}
                continue

            # Try instantiation (skip for abstract classes like device_protocols)
            try:
                instance = found_class()
                component_name = getattr(instance, name_property, "?")
                imports[comp.name] = {
                    "ok": True,
                    "class_name": f"{found_class.__name__} ({name_property}={component_name})",
                }
            except TypeError:
                # Abstract class — can't instantiate, but class was found and imported
                imports[comp.name] = {
                    "ok": True,
                    "class_name": f"{found_class.__name__} (abstract, class found)",
                }

        except Exception as e:
            imports[comp.name] = {"ok": False, "error": str(e)}
        finally:
            sys.path[:] = old_path

    return {"manifest": manifest, "imports": imports}


def _seed_secrets(manifest: CommandManifest) -> None:
    """Seed empty secret rows for the command's declared secrets."""
    if not manifest.secrets:
        return

    try:
        from services.secret_service import set_secret, get_secret_value
    except ImportError as e:
        logger.warning("Could not seed secrets", error=str(e))
        return

    # Per-secret isolation: one bad declaration (e.g. an int secret that can't
    # seed an empty value) must not abort seeding for the rest of the package.
    for secret in manifest.secrets:
        try:
            existing = get_secret_value(secret.key, secret.scope)
            if existing is None:
                set_secret(secret.key, "", secret.scope, secret.value_type)
                logger.info("Seeded empty secret", key=secret.key, scope=secret.scope)
        except Exception as e:
            logger.warning(
                "Could not seed secret", key=secret.key, error=str(e)
            )


def _refresh_discovery_caches() -> None:
    """Refresh all discovery caches after install/remove.

    Without this, in-memory caches hold stale state and subsequent
    installs may hit false name-conflict errors, or newly installed
    components won't be available until the container restarts.
    """
    try:
        from utils.command_discovery_service import get_command_discovery_service
        get_command_discovery_service().refresh_now()
    except Exception as e:
        logger.warning("Command discovery refresh failed (non-fatal)", error=str(e))
    try:
        from utils.agent_discovery_service import get_agent_discovery_service
        get_agent_discovery_service().refresh()
    except Exception as e:
        logger.warning("Agent discovery refresh failed (non-fatal)", error=str(e))
    try:
        from utils.device_family_discovery_service import get_device_family_discovery_service
        get_device_family_discovery_service().refresh()
    except Exception as e:
        logger.warning("Device family discovery refresh failed (non-fatal)", error=str(e))
    try:
        from utils.device_manager_discovery_service import get_device_manager_discovery_service
        get_device_manager_discovery_service().refresh()
    except Exception as e:
        logger.warning("Device manager discovery refresh failed (non-fatal)", error=str(e))


def _cleanup_secrets_for_package(package_name: str) -> None:
    """Delete secrets associated with a package's commands and device protocols before removal.

    Loads commands and device protocols from discovery to get their secret keys,
    then deletes matching rows from the secrets table (all scopes and user_ids).
    """
    try:
        from utils.command_discovery_service import get_command_discovery_service
        from utils.device_family_discovery_service import get_device_family_discovery_service
        from db import SessionLocal
        from sqlalchemy import text

        # Find component names from package metadata
        pkg_meta_path = PACKAGES_DIR / f"{package_name}.json"
        command_names: list[str] = []
        protocol_names: list[str] = []
        if pkg_meta_path.exists():
            with open(pkg_meta_path) as f:
                meta = json.load(f)
            for comp in meta.get("components", []):
                if comp.get("type") == "command":
                    command_names.append(comp["name"])
                elif comp.get("type") == "device_protocol":
                    protocol_names.append(comp["name"])
        if not command_names and not protocol_names:
            command_names = [package_name]
            protocol_names = [package_name]

        # Collect secret keys from matching commands
        secret_keys: set[str] = set()
        service = get_command_discovery_service()
        commands = service.get_all_commands(include_disabled=True)
        for name in command_names:
            cmd = commands.get(name)
            if cmd:
                for secret in cmd.all_possible_secrets:
                    secret_keys.add(secret.key)

        # Collect secret keys from matching device protocols
        family_service = get_device_family_discovery_service()
        families = family_service.get_all_families()
        for name in protocol_names:
            family = families.get(name)
            if family:
                for secret in family.required_secrets:
                    secret_keys.add(secret.key)

        if secret_keys:
            with SessionLocal() as session:
                for key in secret_keys:
                    session.execute(text("DELETE FROM secrets WHERE key = :k"), {"k": key})
                session.commit()
            logger.info("Cleaned up secrets", package=package_name, keys=list(secret_keys))
    except Exception as e:
        logger.warning("Secret cleanup failed (non-fatal)", package=package_name, error=str(e))


def _check_no_dependents(package_name: str) -> None:
    """Check that no installed package depends on this one.

    Scans all ``~/.jarvis/packages/*.json`` for ``jarvis_dependencies``
    entries that include *package_name*. Raises :class:`RemoveError` if
    any are found.
    """
    if not PACKAGES_DIR.exists():
        return
    dependents: list[str] = []
    for meta_file in PACKAGES_DIR.glob("*.json"):
        if meta_file.stem == package_name:
            continue
        try:
            with open(meta_file) as f:
                meta = json.load(f)
            if package_name in meta.get("jarvis_dependencies", []):
                dependents.append(meta.get("package_name", meta_file.stem))
        except Exception:
            continue
    if dependents:
        raise RemoveError(
            f"Cannot remove '{package_name}': the following packages depend on it: "
            f"{', '.join(dependents)}. Remove them first."
        )


def remove(package_name: str, component_type: str | None = None) -> None:
    """Remove an installed package (command or bundle).

    Checks ~/.jarvis/packages/<name>.json for component directories.
    Falls back to legacy component directory lookup.

    Args:
        package_name: The package/command name to remove.
        component_type: Optional component type hint (e.g. "device_protocol")
            to scope the legacy fallback to the correct directory.

    Raises:
        RemoveError: If the package is not found or has dependents.
    """
    # Guard: fail if other packages depend on this one
    _check_no_dependents(package_name)

    # Best-effort cleanup of any systemd drop-ins written by this package's
    # post_install ops. Marker-driven (see _remove_post_install_dropins);
    # failures are logged but never block the uninstall.
    _remove_post_install_dropins(package_name)

    # Collect secret keys to clean up (before removing files)
    _cleanup_secrets_for_package(package_name)

    # Check for package metadata first
    pkg_meta_path = PACKAGES_DIR / f"{package_name}.json"
    if pkg_meta_path.exists():
        with open(pkg_meta_path) as f:
            pkg_meta = json.load(f)

        component_dirs = pkg_meta.get("component_dirs", {})
        logger.info("Removing package", package=package_name, components=len(component_dirs))

        for comp_name, comp_dir_str in component_dirs.items():
            comp_dir = Path(comp_dir_str)
            if comp_dir.exists():
                # Safety: must be under project dir
                if not str(comp_dir.resolve()).startswith(str(_PROJECT_DIR.resolve())):
                    logger.warning("Skipping unsafe path", path=comp_dir_str)
                    continue
                shutil.rmtree(comp_dir)
                logger.info("Removed component dir", component=comp_name, dir=comp_dir_str)

        # Defensive sweep: reconstruct install dirs from the declared components
        # list and remove any that linger. Why: older installs (and partial
        # metadata writes) sometimes recorded only a subset of components in
        # `component_dirs` — most commonly the command, omitting bundled
        # agents/protocols. Without this sweep those orphans stay on disk after
        # uninstall, the agent scheduler keeps trying to run them, and you get
        # CPU-burning ImportError loops (see jarvis-node spotify_keepalive
        # incident, May 2026). Idempotent: dirs removed above are skipped.
        for comp in pkg_meta.get("components", []):
            comp_type = comp.get("type")
            comp_decl_name = comp.get("name")
            if not comp_type or not comp_decl_name:
                continue
            install_rel = COMPONENT_INSTALL_DIRS.get(comp_type)
            if not install_rel:
                continue
            install_dir = _PROJECT_DIR / install_rel / comp_decl_name
            if not install_dir.exists() or not install_dir.is_dir():
                continue
            # Safety: must be under project dir (defense in depth)
            if not str(install_dir.resolve()).startswith(str(_PROJECT_DIR.resolve())):
                continue
            shutil.rmtree(install_dir)
            logger.info(
                "Removed orphan component dir (defensive sweep)",
                component=f"{comp_type}:{comp_decl_name}",
                dir=str(install_dir),
            )

        # Remove shared lib dir and auto-generated namespace
        pkg_dir = PACKAGES_DIR / package_name
        # Also drop any rollback snapshot — an orphaned .previous would let
        # a future fresh install "roll back" to a long-uninstalled version
        # (and would block the empty-dir cleanup below).
        prev_dir = pkg_dir / ".previous"
        if prev_dir.exists():
            shutil.rmtree(prev_dir, ignore_errors=True)
        lib_dir = pkg_dir / f"{package_name}_lib"
        if lib_dir.exists():
            shutil.rmtree(lib_dir)
        # Backwards compat: older installs used a bare "lib" dir name.
        legacy_lib_dir = pkg_dir / "lib"
        if legacy_lib_dir.exists():
            shutil.rmtree(legacy_lib_dir)
        namespace_init = pkg_dir / "__init__.py"
        if namespace_init.exists():
            namespace_init.unlink()
        # Remove package dir if empty
        if pkg_dir.exists() and not any(pkg_dir.iterdir()):
            pkg_dir.rmdir()

        # Remove package metadata
        pkg_meta_path.unlink()

        # Disable this package's commands. But a command that overrode an
        # *overridable* built-in (same name) must RE-ENABLE that built-in, so
        # removing the package restores it (e.g. direct device control returns
        # when the Home Assistant bundle is uninstalled).
        _overridable_builtins = _get_overridable_builtin_command_names()
        for comp in pkg_meta.get("components", []):
            if comp.get("type") == "command":
                if comp["name"] in _overridable_builtins:
                    _enable_in_registry(comp["name"])
                else:
                    _disable_in_registry(comp["name"])

        logger.info("Package removed", package=package_name)
        _refresh_discovery_caches()
        return

    # Legacy fallback: check custom component directories.
    # If component_type is provided, only check that specific directory;
    # otherwise check all directories (but risk name collisions).
    search_dirs = (
        {component_type: COMPONENT_INSTALL_DIRS[component_type]}
        if component_type and component_type in COMPONENT_INSTALL_DIRS
        else COMPONENT_INSTALL_DIRS
    )
    for comp_type, rel_dir in search_dirs.items():
        install_dir = _PROJECT_DIR / rel_dir / package_name
        if install_dir.exists():
            parent_dir = _PROJECT_DIR / rel_dir
            if not str(install_dir.resolve()).startswith(str(parent_dir.resolve())):
                raise RemoveError(f"Cannot remove: path escapes {rel_dir} directory")

            logger.info("Removing custom component (legacy)", type=comp_type, name=package_name)
            if comp_type == "command":
                _disable_in_registry(package_name)
            shutil.rmtree(install_dir)
            logger.info("Custom component removed", type=comp_type, name=package_name)
            _refresh_discovery_caches()
            return

    raise RemoveError(f"Package '{package_name}' is not installed")


def revert_package(package_name: str) -> str:
    """Restore the previously-installed version from its .previous snapshot.

    Callable from the package-revert MQTT flow and from the boot-time
    auto-rollback. The caller is responsible for restarting the node — the
    bad version's modules stay loaded in this process until then.

    Steps:
    1. Remove the current component dirs (recorded ``component_dirs`` plus
       the same defensive sweep and path-safety checks as :func:`remove`).
    2. Restore component dirs, shared lib, namespace, and metadata from
       ``.previous``.
    3. Best-effort ``pip install`` of the restored metadata's declared pip
       deps (no uninstalls — extra dists from the bad version are harmless).
    4. Delete the consumed ``.previous`` (we keep N-1, and N-1 is now live).

    Returns:
        The restored version string.

    Raises:
        RevertError: when the package has no metadata or no .previous
            snapshot.
    """
    meta_path = PACKAGES_DIR / f"{package_name}.json"
    if not meta_path.exists():
        raise RevertError(f"Package '{package_name}' is not installed")

    pkg_dir = PACKAGES_DIR / package_name
    prev_dir = pkg_dir / ".previous"
    prev_meta_path = prev_dir / "meta.json"
    if not prev_meta_path.exists():
        raise RevertError(
            f"No previous version of '{package_name}' to revert to"
        )

    with open(meta_path) as f:
        current_meta = json.load(f)
    with open(prev_meta_path) as f:
        prev_meta = json.load(f)

    logger.info(
        "Reverting package",
        package=package_name,
        from_version=current_meta.get("version"),
        to_version=prev_meta.get("version"),
    )

    # 1. Remove current component dirs — recorded dirs + the defensive
    # sweep over declared components, with the same under-project-dir
    # safety checks as remove().
    for comp_name, comp_dir_str in (current_meta.get("component_dirs") or {}).items():
        comp_dir = Path(comp_dir_str)
        if not comp_dir.exists():
            continue
        if not str(comp_dir.resolve()).startswith(str(_PROJECT_DIR.resolve())):
            logger.warning("Skipping unsafe path", path=comp_dir_str)
            continue
        shutil.rmtree(comp_dir)
    for comp in current_meta.get("components", []):
        comp_type = comp.get("type")
        comp_decl_name = comp.get("name")
        if not comp_type or not comp_decl_name:
            continue
        install_rel = COMPONENT_INSTALL_DIRS.get(comp_type)
        if not install_rel:
            continue
        install_dir = _PROJECT_DIR / install_rel / comp_decl_name
        if not install_dir.exists() or not install_dir.is_dir():
            continue
        if not str(install_dir.resolve()).startswith(str(_PROJECT_DIR.resolve())):
            continue
        shutil.rmtree(install_dir)

    # 2a. Restore component dirs to the locations the old metadata recorded.
    components_dir = prev_dir / "components"
    for comp_key, comp_dir_str in (prev_meta.get("component_dirs") or {}).items():
        src = components_dir / comp_key.replace(":", "__")
        if not src.is_dir():
            # Defense in depth: the snapshot writer is atomic (meta.json
            # exists only for complete snapshots), so a recorded component
            # missing from .previous/components/ means corruption.
            # Restoring partially would silently drop components — refuse.
            raise RevertError(
                f"Saved previous version of '{package_name}' is incomplete "
                f"(missing {comp_key}) — cannot revert."
            )
        dest = Path(comp_dir_str)
        if not str(dest.resolve()).startswith(str(_PROJECT_DIR.resolve())):
            logger.warning("Skipping unsafe restore path", path=comp_dir_str)
            continue
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)

    # 2b. Restore shared lib dir(s) and the namespace __init__.py. When the
    # old version had no lib/namespace, the current ones are removed.
    for lib_name in (f"{package_name}_lib", "lib"):
        current_lib = pkg_dir / lib_name
        if current_lib.is_dir():
            shutil.rmtree(current_lib)
        prev_lib = prev_dir / lib_name
        if prev_lib.is_dir():
            shutil.copytree(prev_lib, current_lib)

    ns_init = pkg_dir / "__init__.py"
    if ns_init.exists():
        ns_init.unlink()
    prev_init = prev_dir / "__init__.py"
    if prev_init.exists():
        shutil.copy2(prev_init, ns_init)

    # 2c. Restore metadata. Strip any stale previous_version — this revert
    # consumes the .previous snapshot, so there's nothing older to offer.
    prev_meta.pop("previous_version", None)
    with open(meta_path, "w") as f:
        json.dump(prev_meta, f, indent=2)

    # 3. Best-effort pip install of the restored version's declared deps.
    deps: list[str] = []
    for pkg in prev_meta.get("pip_packages") or []:
        name = pkg.get("name")
        version = pkg.get("version")
        if not name:
            continue
        if version:
            deps.append(f"{name}=={version}" if version[0].isdigit() else f"{name}{version}")
        else:
            deps.append(name)
    if deps:
        try:
            result = subprocess.run(
                ["nice", "-n", "15", sys.executable, "-m", "pip", "install",
                 "--quiet", "--prefer-binary"] + deps,
                capture_output=True,
                text=True,
                timeout=600,
            )
            if result.returncode != 0:
                logger.warning(
                    "pip install during revert failed (non-fatal)",
                    package=package_name,
                    stderr=(result.stderr or "").strip()[:300],
                )
        except Exception as e:
            logger.warning(
                "pip install during revert failed (non-fatal)",
                package=package_name,
                error=str(e),
            )

    # 4. Drop the consumed snapshot — revert is one-shot.
    shutil.rmtree(prev_dir, ignore_errors=True)

    restored_version = str(prev_meta.get("version") or "unknown")
    logger.info("Package reverted", package=package_name, version=restored_version)
    return restored_version


def _enable_in_registry(command_name: str) -> None:
    """Enable a command in the node's command registry."""
    try:
        from db import SessionLocal
        from repositories.command_registry_repository import CommandRegistryRepository
        db = SessionLocal()
        try:
            repo = CommandRegistryRepository(db)
            repo.set_enabled(command_name, True)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("Could not update command registry", error=str(e))


def _disable_in_registry(command_name: str) -> None:
    """Disable a command in the node's command registry."""
    try:
        from db import SessionLocal
        from repositories.command_registry_repository import CommandRegistryRepository
        db = SessionLocal()
        try:
            repo = CommandRegistryRepository(db)
            repo.set_enabled(command_name, False)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("Could not update command registry", error=str(e))


def list_installed() -> list[dict[str, Any]]:
    """List all installed packages and custom commands.

    Reads from ~/.jarvis/packages/ first, then falls back
    to scanning custom_commands/ for legacy installs.

    Returns:
        List of dicts with package/command metadata.
    """
    results: list[dict[str, Any]] = []
    seen_names: set[str] = set()

    # 1. Package metadata files
    if PACKAGES_DIR.exists():
        for meta_file in sorted(PACKAGES_DIR.glob("*.json")):
            with open(meta_file) as f:
                metadata = json.load(f)
            results.append(metadata)
            seen_names.add(metadata.get("package_name", ""))

    # 2. Legacy custom_commands/ scan
    if CUSTOM_COMMANDS_DIR.exists():
        for entry in sorted(CUSTOM_COMMANDS_DIR.iterdir()):
            if not entry.is_dir() or entry.name.startswith("_"):
                continue
            if entry.name in seen_names:
                continue

            meta_file = entry / STORE_METADATA_FILE
            if meta_file.exists():
                with open(meta_file) as f:
                    metadata = json.load(f)
                results.append(metadata)
            else:
                results.append({
                    "command_name": entry.name,
                    "version": "unknown",
                    "repo_url": None,
                    "installed_at": None,
                })

    return results


def get_installed_metadata(package_name: str) -> dict[str, Any] | None:
    """Get metadata for an installed package or custom command."""
    # Check package metadata first
    pkg_meta = PACKAGES_DIR / f"{package_name}.json"
    if pkg_meta.exists():
        with open(pkg_meta) as f:
            return json.load(f)

    # Legacy fallback
    meta_file = CUSTOM_COMMANDS_DIR / package_name / STORE_METADATA_FILE
    if meta_file.exists():
        with open(meta_file) as f:
            return json.load(f)
    return None

