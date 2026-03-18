"""Local command store operations: install, remove, update, list.

Manages custom commands installed from GitHub repos or the command store API.
Commands are installed to commands/custom_commands/<command_name>/.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jarvis_log_client import JarvisLogger

from core.command_manifest import CommandManifest

logger = JarvisLogger(service="jarvis-node")

# Path to custom commands directory
CUSTOM_COMMANDS_DIR = Path(__file__).resolve().parent.parent / "commands" / "custom_commands"

# Metadata file written alongside installed commands
STORE_METADATA_FILE = ".store_metadata.json"


class CommandStoreError(Exception):
    """Base exception for command store operations."""


class InstallError(CommandStoreError):
    """Error during command installation."""


class RemoveError(CommandStoreError):
    """Error during command removal."""


def _clone_repo(repo_url: str, tag: str | None = None) -> Path:
    """Clone a GitHub repo to a temp directory.

    Args:
        repo_url: GitHub HTTPS URL.
        tag: Optional git tag to checkout.

    Returns:
        Path to the cloned directory.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="jarvis-cmd-"))
    cmd = ["git", "clone", "--depth", "1"]
    if tag:
        cmd.extend(["--branch", tag])
    cmd.extend([repo_url, str(tmpdir / "repo")])

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if result.returncode != 0:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise InstallError(f"git clone failed: {result.stderr.strip()}")

    return tmpdir / "repo"


def _validate_repo_structure(repo_dir: Path) -> CommandManifest:
    """Validate the repo has required files and a valid manifest.

    Returns:
        Parsed CommandManifest.
    """
    required_files = ["jarvis_command.yaml", "command.py"]
    for fname in required_files:
        if not (repo_dir / fname).exists():
            raise InstallError(f"Missing required file: {fname}")

    manifest_path = repo_dir / "jarvis_command.yaml"
    with open(manifest_path) as f:
        raw = yaml.safe_load(f)

    try:
        manifest = CommandManifest(**raw)
    except Exception as e:
        raise InstallError(f"Invalid manifest: {e}")

    if not manifest.name:
        raise InstallError("Manifest 'name' is empty")

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


def _check_name_conflict(command_name: str) -> None:
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


def install_from_github(
    repo_url: str,
    version_tag: str | None = None,
    skip_tests: bool = False,
) -> CommandManifest:
    """Install a command from a GitHub repo URL.

    Args:
        repo_url: GitHub HTTPS URL (e.g. https://github.com/user/jarvis-command-foo).
        version_tag: Optional git tag to checkout (e.g. "v1.0.0").
        skip_tests: Skip container tests (user accepts risk).

    Returns:
        The installed command's manifest.
    """
    logger.info("Installing command from GitHub", repo_url=repo_url, tag=version_tag)

    # 1. Clone
    repo_dir = _clone_repo(repo_url, version_tag)
    try:
        # 2. Validate structure + manifest
        manifest = _validate_repo_structure(repo_dir)

        # 3. Check platform
        _check_platform_compatibility(manifest)

        # 4. Check name conflict
        _check_name_conflict(manifest.name)

        # 5. Container tests (Phase 5 — skipped for now)
        if not skip_tests:
            # TODO: Run container tests via container_test_service
            pass

        # 6. Install to custom_commands/
        install_dir = CUSTOM_COMMANDS_DIR / manifest.name
        if install_dir.exists():
            logger.warning("Replacing existing custom command", command=manifest.name)
            shutil.rmtree(install_dir)

        install_dir.mkdir(parents=True, exist_ok=True)

        # Copy command.py
        shutil.copy2(repo_dir / "command.py", install_dir / "command.py")

        # Copy requirements.txt if present
        req_file = repo_dir / "requirements.txt"
        if req_file.exists():
            shutil.copy2(req_file, install_dir / "requirements.txt")

        # Copy any extra Python files referenced by command.py
        for py_file in repo_dir.glob("*.py"):
            if py_file.name != "command.py":
                shutil.copy2(py_file, install_dir / py_file.name)

        # Copy manifest
        shutil.copy2(repo_dir / "jarvis_command.yaml", install_dir / "jarvis_command.yaml")

        # 7. Write __init__.py
        (install_dir / "__init__.py").write_text(
            f"# Custom command: {manifest.name}\n"
        )

        # 8. Write metadata
        _write_store_metadata(install_dir, manifest, repo_url)

        # 9. Install pip deps
        _install_pip_deps(manifest)

        # 10. Seed secrets
        _seed_secrets(manifest)

        logger.info(
            "Command installed successfully",
            command=manifest.name,
            version=manifest.version,
        )
        return manifest

    finally:
        # Clean up temp dir
        shutil.rmtree(repo_dir.parent, ignore_errors=True)


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


def remove(command_name: str) -> None:
    """Remove an installed custom command.

    Args:
        command_name: The command_name to remove.

    Raises:
        RemoveError: If the command is not found or is a built-in.
    """
    install_dir = CUSTOM_COMMANDS_DIR / command_name
    if not install_dir.exists():
        raise RemoveError(f"Custom command '{command_name}' is not installed")

    # Safety check — ensure it's in custom_commands
    if not str(install_dir.resolve()).startswith(str(CUSTOM_COMMANDS_DIR.resolve())):
        raise RemoveError("Cannot remove: path escapes custom_commands directory")

    logger.info("Removing custom command", command=command_name)

    # Disable in command registry
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

    shutil.rmtree(install_dir)
    logger.info("Custom command removed", command=command_name)


def list_installed() -> list[dict[str, Any]]:
    """List all installed custom commands.

    Returns:
        List of dicts with command metadata.
    """
    results: list[dict[str, Any]] = []

    if not CUSTOM_COMMANDS_DIR.exists():
        return results

    for entry in sorted(CUSTOM_COMMANDS_DIR.iterdir()):
        if not entry.is_dir() or entry.name.startswith("_"):
            continue

        meta_file = entry / STORE_METADATA_FILE
        if meta_file.exists():
            with open(meta_file) as f:
                metadata = json.load(f)
            results.append(metadata)
        else:
            # No metadata file — manually installed or legacy
            results.append({
                "command_name": entry.name,
                "version": "unknown",
                "repo_url": None,
                "installed_at": None,
            })

    return results


def get_installed_metadata(command_name: str) -> dict[str, Any] | None:
    """Get metadata for an installed custom command."""
    meta_file = CUSTOM_COMMANDS_DIR / command_name / STORE_METADATA_FILE
    if meta_file.exists():
        with open(meta_file) as f:
            return json.load(f)
    return None


# Need sys for pip subprocess
import sys  # noqa: E402
