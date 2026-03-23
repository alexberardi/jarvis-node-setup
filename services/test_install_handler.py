"""Handle test install requests from CC via MQTT.

Zero-trust flow:
1. CC publishes MQTT nudge with just request_id (no code, no URLs)
2. This handler verifies with CC: GET /nodes/{node_id}/test-install/{request_id}/verify
3. CC confirms and returns Pantry download URL
4. Handler downloads files from Pantry, installs to commands/test_commands/{package_name}/
5. Pip deps diffed before/after, tracked in .test_meta.json for cleanup
6. Results POSTed back to CC for mobile to poll
"""

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

_PROJECT_DIR = Path(__file__).resolve().parent.parent
_TEST_COMMANDS_DIR = _PROJECT_DIR / "commands" / "test_commands"
_TEST_TTL_MINUTES = 20


def run_test_install_and_upload(request_id: str) -> None:
    """Verify with CC, download from Pantry, install, upload result. Runs in background thread."""
    try:
        # 1. Verify with CC (zero-trust gate)
        verify_data = _verify_with_cc(request_id)
        if not verify_data:
            return

        package_name: str = verify_data["package_name"]
        pantry_url: str = verify_data["pantry_download_url"]

        logger.info(
            "Test install verified",
            request_id=request_id[:8],
            package=package_name,
        )

        # 2. Download files from Pantry
        draft_data = RestClient.get(pantry_url, timeout=15)
        if not draft_data:
            _upload_result(request_id, success=False, error="Failed to download from Pantry")
            return

        files: list[dict[str, str]] = draft_data.get("files", [])
        if not files:
            _upload_result(request_id, success=False, error="No files in draft")
            return

        # 3. Snapshot pip packages before install
        before_packages = _pip_freeze()

        # 4. Write files to test_commands/{package_name}/
        install_dir = _TEST_COMMANDS_DIR / package_name
        _write_test_command(install_dir, files)

        # 5. Install pip deps if requirements.txt exists
        req_file = install_dir / "requirements.txt"
        if req_file.exists():
            _install_pip_deps(req_file)

        # 6. Snapshot pip packages after install, compute diff
        after_packages = _pip_freeze()
        pip_added = sorted(after_packages - before_packages)

        # 7. Write .test_meta.json
        now = datetime.now(timezone.utc)
        meta = {
            "package_name": package_name,
            "installed_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=_TEST_TTL_MINUTES)).isoformat(),
            "pip_packages_added": pip_added,
            "files": [f.get("filename", "") for f in files],
        }

        meta_path = install_dir / ".test_meta.json"
        meta_path.write_text(json.dumps(meta, indent=2))

        # 8. Refresh command discovery
        try:
            from utils.command_discovery_service import get_command_discovery_service
            get_command_discovery_service().refresh_now()
            logger.info("Command discovery refreshed after test install")
        except Exception as e:
            logger.warning("Command discovery refresh failed (non-fatal)", error=str(e))

        # 9. Upload success
        _upload_result(request_id, success=True, details={
            "package_name": package_name,
            "files_installed": len(files),
            "pip_packages_added": len(pip_added),
        })

        logger.info(
            "Test install completed",
            request_id=request_id[:8],
            package=package_name,
            files=len(files),
            pip_added=len(pip_added),
        )

    except Exception as e:
        logger.error("Test install failed", request_id=request_id[:8], error=str(e))
        _upload_result(request_id, success=False, error=str(e))


def _verify_with_cc(request_id: str) -> dict[str, Any] | None:
    """Verify test install request with CC. Returns verify response or None."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot verify test install: CC URL not resolved")
        return None

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/test-install/{request_id}/verify"
    result = RestClient.get(url, timeout=10)
    if not result or not result.get("confirmed"):
        logger.warning("Test install verification failed", request_id=request_id[:8])
        _upload_result(request_id, success=False, error="Verification failed")
        return None

    return result


def _write_test_command(install_dir: Path, files: list[dict[str, str]]) -> None:
    """Write files to the test command directory."""
    # Ensure test_commands dir exists with __init__.py
    _TEST_COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    init_file = _TEST_COMMANDS_DIR / "__init__.py"
    if not init_file.exists():
        init_file.write_text("# Test commands (auto-cleaned after 20 minutes)\n")

    # Clean previous install of same package
    if install_dir.exists():
        shutil.rmtree(install_dir)
    install_dir.mkdir(parents=True)

    # Write files
    for file_data in files:
        filename = file_data.get("filename", "")
        content = file_data.get("content", "")
        if not filename:
            continue
        file_path = install_dir / filename
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content)

    # Ensure __init__.py exists
    pkg_init = install_dir / "__init__.py"
    if not pkg_init.exists():
        pkg_init.write_text(f"# Test install: {install_dir.name}\n")


def _pip_freeze() -> set[str]:
    """Get current pip packages as a set."""
    try:
        output = subprocess.check_output(
            [sys.executable, "-m", "pip", "freeze", "--quiet"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        return set(output.strip().splitlines())
    except Exception:
        return set()


def _install_pip_deps(requirements_file: Path) -> None:
    """Install pip dependencies from requirements.txt."""
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "-r", str(requirements_file), "--quiet"],
            stderr=subprocess.DEVNULL,
        )
        logger.info("Pip dependencies installed", requirements=str(requirements_file))
    except subprocess.CalledProcessError as e:
        logger.warning("Pip install failed (non-fatal)", error=str(e))


def _upload_result(
    request_id: str,
    success: bool,
    error: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """POST test install result to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload test install result: CC URL not resolved")
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/test-install/{request_id}/results"

    payload: dict[str, Any] = {"success": success}
    if error:
        payload["error"] = error
    if details:
        payload["details"] = details

    result = RestClient.post(url, data=payload, timeout=15)
    if result is not None:
        logger.info("Test install result uploaded", request_id=request_id[:8], success=success)
    else:
        logger.error("Failed to upload test install result", request_id=request_id[:8])
