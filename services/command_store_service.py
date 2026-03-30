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
    code from their bundle's lib directory.
    """
    if not PACKAGES_DIR.exists():
        return

    for meta_file in PACKAGES_DIR.glob("*.json"):
        lib_dir = meta_file.parent / meta_file.stem / "lib"
        if lib_dir.is_dir() and str(lib_dir) not in sys.path:
            sys.path.append(str(lib_dir))
            logger.debug("Added package lib to path", package=meta_file.stem, lib=str(lib_dir))


class CommandStoreError(Exception):
    """Base exception for command store operations."""


class InstallError(CommandStoreError):
    """Error during command installation."""


class RemoveError(CommandStoreError):
    """Error during command removal."""


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


def _clone_repo(repo_url: str, tag: str | None = None) -> Path:
    """Download a repo — tries archive download first, falls back to git clone.

    Args:
        repo_url: GitHub HTTPS URL.
        tag: Optional git tag to checkout.

    Returns:
        Path to the repo directory.
    """
    # Try archive download first (no git/auth needed)
    result_path = _download_archive(repo_url, tag)
    if result_path:
        return result_path

    # Fallback to git clone
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cmd-"))
    cmd = ["git", "clone", "--depth", "1"]
    if tag:
        cmd.extend(["--branch", tag])
    cmd.extend([repo_url, str(tmpdir / "repo")])

    # Prevent git from prompting for credentials (hangs headless nodes)
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, env=env)
    if result.returncode != 0 and tag:
        shutil.rmtree(tmpdir, ignore_errors=True)
        tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cmd-"))
        fallback_cmd = ["git", "clone", "--depth", "1", repo_url, str(tmpdir / "repo")]
        result = subprocess.run(fallback_cmd, capture_output=True, text=True, timeout=60, env=env)
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise InstallError(f"Failed to download package: {result.stderr.strip()}")

    return tmpdir / "repo"


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


def _check_command_name_conflict(command_name: str) -> None:
    """Check if the command name conflicts with a built-in command."""
    try:
        import commands
        import importlib
        import pkgutil

        for _, mod_name, _ in pkgutil.iter_modules(commands.__path__):
            try:
                module = importlib.import_module(f"commands.{mod_name}")
                from core.ijarvis_command import IJarvisCommand
                for attr in dir(module):
                    cls = getattr(module, attr)
                    if (isinstance(cls, type)
                            and issubclass(cls, IJarvisCommand)
                            and cls is not IJarvisCommand):
                        instance = cls()
                        if instance.command_name == command_name:
                            raise InstallError(
                                f"Command name '{command_name}' conflicts with "
                                f"built-in command in commands/{mod_name}.py"
                            )
            except InstallError:
                raise
            except Exception:
                continue
    except ImportError:
        pass  # Not in node context


def _check_agent_name_conflict(agent_name: str) -> None:
    """Check if the agent name conflicts with a built-in agent."""
    try:
        from utils.agent_discovery_service import get_agent_discovery_service
        svc = get_agent_discovery_service()
        existing = svc.get_agent(agent_name)
        if existing:
            raise InstallError(f"Agent name '{agent_name}' conflicts with built-in agent")
    except ImportError:
        pass


def _check_protocol_name_conflict(protocol_name: str) -> None:
    """Check if the protocol name conflicts with a built-in device protocol."""
    try:
        from utils.device_family_discovery_service import get_device_family_discovery_service
        svc = get_device_family_discovery_service()
        existing = svc.get_family(protocol_name)
        if existing:
            raise InstallError(f"Protocol name '{protocol_name}' conflicts with built-in protocol")
    except ImportError:
        pass


def _install_pip_deps(manifest: CommandManifest) -> None:
    """Install pip dependencies declared in the manifest."""
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
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--quiet"] + deps,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise InstallError(f"pip install failed: {result.stderr.strip()}")


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

    Shared code lives at ~/.jarvis/packages/<name>/lib/ and is added to
    sys.path at node startup so all scattered components can import from it.

    Args:
        package_name: The package name.
        shared_dirs: Shared directories from the repo to install.
        repo_dir: Cloned repo root (for copying root-level .py files).

    Returns:
        The shared lib directory.
    """
    lib_dir = PACKAGES_DIR / package_name / "lib"
    if lib_dir.exists():
        shutil.rmtree(lib_dir)
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
        shutil.rmtree(install_dir)

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
) -> None:
    """Write package metadata to ~/.jarvis/packages/<name>.json.

    This tracks all component install dirs for clean uninstall.
    """
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
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
    }
    meta_path = PACKAGES_DIR / f"{manifest.name}.json"
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)


def _do_install(repo_dir: Path, source_label: str) -> CommandManifest:
    """Core install logic — validates, scatters components, installs deps.

    Args:
        repo_dir: Path to repo directory (cloned or local).
        source_label: Display label for logging (URL or path).

    Returns:
        The installed manifest.
    """
    # 1. Validate structure + manifest
    manifest = _validate_repo_structure(repo_dir)

    # 2. Check platform
    _check_platform_compatibility(manifest)

    # 3. Check name conflicts for all components
    _check_name_conflicts(manifest)

    # 4. Install shared code for bundles (ha_shared/, etc.)
    component_paths = [c.path for c in manifest.components]
    shared_dirs = _collect_shared_dirs(repo_dir, component_paths) if manifest.is_bundle else []
    if shared_dirs:
        _install_shared_code(manifest.name, shared_dirs, repo_dir)

    # 5. Install components
    component_dirs: dict[str, str] = {}
    for comp in manifest.components:
        install_dir = _install_component(repo_dir, comp.type, comp.name, comp.path)
        # Use type:name as key to avoid collisions (e.g., agent and manager both named "home_assistant")
        component_dirs[f"{comp.type}:{comp.name}"] = str(install_dir)

    # 6. Write package metadata (for clean uninstall)
    _write_package_metadata(manifest, source_label, component_dirs)

    # 7. Also write .store_metadata.json in the first command dir
    command_comps = [c for c in manifest.components if c.type == "command"]
    if command_comps:
        first_cmd_dir = _PROJECT_DIR / COMPONENT_INSTALL_DIRS["command"] / command_comps[0].name
        if first_cmd_dir.exists():
            _write_store_metadata(first_cmd_dir, manifest, source_label)

    # 8. Install pip deps
    _install_pip_deps(manifest)

    # 9. Seed secrets
    _seed_secrets(manifest)

    # 10. Enable commands in registry
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
) -> CommandManifest:
    """Install a command or bundle from a GitHub repo URL.

    Args:
        repo_url: GitHub HTTPS URL.
        version_tag: Optional git tag to checkout.
        skip_tests: Skip container tests (user accepts risk).

    Returns:
        The installed command's manifest.
    """
    logger.info("Installing from GitHub", repo_url=repo_url, tag=version_tag)
    repo_dir = _clone_repo(repo_url, version_tag)
    try:
        return _do_install(repo_dir, repo_url)
    finally:
        shutil.rmtree(repo_dir.parent, ignore_errors=True)


def install_from_local(local_path: str | Path) -> CommandManifest:
    """Install a command or bundle from a local directory.

    Useful for development and testing. Does not clone — reads directly
    from the given path.

    Args:
        local_path: Path to a directory containing a manifest + components.

    Returns:
        The installed command's manifest.
    """
    repo_dir = Path(local_path).resolve()
    if not repo_dir.is_dir():
        raise InstallError(f"Not a directory: {repo_dir}")
    logger.info("Installing from local path", path=str(repo_dir))
    return _do_install(repo_dir, f"local:{repo_dir}")


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

        # Add repo root and lib dir to sys.path for imports
        paths_to_add = [str(repo_dir)]
        lib_dir = repo_dir / "lib"
        if lib_dir.is_dir():
            paths_to_add.append(str(lib_dir))

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
        from services.secret_service import seed_command_secrets_from_list
        seed_command_secrets_from_list(manifest.secrets)
    except ImportError:
        # seed_command_secrets_from_list may not exist yet, try manual approach
        try:
            from services.secret_service import set_secret, get_secret_value
            for secret in manifest.secrets:
                existing = get_secret_value(secret.key, secret.scope)
                if existing is None:
                    set_secret(secret.key, "", secret.scope, secret.value_type)
                    logger.info("Seeded empty secret", key=secret.key, scope=secret.scope)
        except Exception as e:
            logger.warning("Could not seed secrets", error=str(e))


def _refresh_discovery_caches() -> None:
    """Refresh command and agent discovery caches after install/remove.

    Without this, in-memory caches hold stale state and subsequent
    installs may hit false name-conflict errors.
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


def remove(package_name: str) -> None:
    """Remove an installed package (command or bundle).

    Checks ~/.jarvis/packages/<name>.json for component directories.
    Falls back to legacy custom_commands/ lookup for single commands.

    Args:
        package_name: The package/command name to remove.

    Raises:
        RemoveError: If the package is not found.
    """
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

        # Remove shared lib dir
        lib_dir = PACKAGES_DIR / package_name / "lib"
        if lib_dir.exists():
            shutil.rmtree(lib_dir)
        # Remove package dir if empty
        pkg_dir = PACKAGES_DIR / package_name
        if pkg_dir.exists() and not any(pkg_dir.iterdir()):
            pkg_dir.rmdir()

        # Remove package metadata
        pkg_meta_path.unlink()

        # Disable commands in registry
        for comp in pkg_meta.get("components", []):
            if comp.get("type") == "command":
                _disable_in_registry(comp["name"])

        logger.info("Package removed", package=package_name)
        _refresh_discovery_caches()
        return

    # Legacy fallback: single command in custom_commands/
    install_dir = CUSTOM_COMMANDS_DIR / package_name
    if not install_dir.exists():
        raise RemoveError(f"Package '{package_name}' is not installed")

    if not str(install_dir.resolve()).startswith(str(CUSTOM_COMMANDS_DIR.resolve())):
        raise RemoveError("Cannot remove: path escapes custom_commands directory")

    logger.info("Removing custom command (legacy)", command=package_name)
    _disable_in_registry(package_name)
    shutil.rmtree(install_dir)
    logger.info("Custom command removed", command=package_name)
    _refresh_discovery_caches()


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

