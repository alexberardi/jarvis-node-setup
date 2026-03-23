"""Background cleanup agent for expired test commands.

Runs every 20 minutes. For each test command in commands/test_commands/:
1. Reads .test_meta.json to check expiry
2. If expired: uninstalls pip packages that were added, removes directory
3. Refreshes command discovery so expired commands disappear
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

_TEST_COMMANDS_DIR = Path(__file__).resolve().parent.parent / "commands" / "test_commands"


def cleanup_expired_test_commands() -> int:
    """Remove expired test commands and their pip deps. Returns count removed."""
    if not _TEST_COMMANDS_DIR.exists():
        return 0

    now = datetime.now(timezone.utc)
    removed = 0

    for entry in _TEST_COMMANDS_DIR.iterdir():
        if not entry.is_dir() or entry.name.startswith("_"):
            continue

        meta_path = entry / ".test_meta.json"
        if not meta_path.exists():
            # No metadata — remove stale directory
            logger.warning("Removing test command without metadata", path=entry.name)
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            continue

        try:
            meta = json.loads(meta_path.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("Invalid test_meta.json, removing", path=entry.name)
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            continue

        expires_at_str = meta.get("expires_at", "")
        try:
            expires_at = datetime.fromisoformat(expires_at_str)
        except (ValueError, TypeError):
            logger.warning("Invalid expires_at in test_meta.json, removing", path=entry.name)
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
            continue

        if expires_at > now:
            continue  # Not expired yet

        package_name = meta.get("package_name", entry.name)
        pip_packages = meta.get("pip_packages_added", [])

        # Uninstall pip packages that were added by this test install
        if pip_packages:
            _uninstall_pip_packages(pip_packages, package_name)

        # Remove the directory
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
        logger.info(
            "Expired test command removed",
            package=package_name,
            pip_removed=len(pip_packages),
        )

    # Refresh command discovery if anything was removed
    if removed > 0:
        try:
            from utils.command_discovery_service import get_command_discovery_service
            get_command_discovery_service().refresh_now()
            logger.info("Command discovery refreshed after test cleanup", removed=removed)
        except Exception as e:
            logger.warning("Command discovery refresh failed after cleanup", error=str(e))

    return removed


def _uninstall_pip_packages(packages: list[str], package_name: str) -> None:
    """Uninstall pip packages. Best-effort — failures are logged but not fatal."""
    # Extract package names (strip ==version)
    names = [p.split("==")[0] for p in packages if p]
    if not names:
        return

    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "uninstall", "-y", *names],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        logger.info("Pip packages uninstalled", package=package_name, count=len(names))
    except subprocess.CalledProcessError as e:
        logger.warning("Pip uninstall failed (non-fatal)", package=package_name, error=str(e))
