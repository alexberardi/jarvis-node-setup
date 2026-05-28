"""Handle package install requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/package-install with request_id + repo info
2. This handler runs install_from_github() from command_store_service
3. Results are POSTed back to CC for mobile to poll

When the install pip-installed new top-level dependencies, the long-running
Python process can't pick them up — a failed ``ImportError`` at boot caches
``None`` for the import target. We handle this by deferring the result POST
to a marker file and self-exiting with non-zero so systemd respawns the
service. ``flush_post_restart_install_result()`` is called from main.py
during boot to read the marker and complete the round-trip — that's also
why mobile only sees "Done" once the new process is fully ready, and why
the wake-word path never engages during the restart window (voice listener
isn't started until after the flush).
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

# Marker file: read by flush_post_restart_install_result() on the next boot
# to complete the install round-trip after a pip-dep-triggered restart.
_PENDING_RESULT_FILE = Path.home() / ".jarvis" / ".pending_install_result.json"


def run_install_and_upload(
    request_id: str,
    command_name: str,
    github_repo_url: str,
    git_tag: str | None,
) -> None:
    """Run package install and upload results to CC. Meant to run in a background thread."""
    print(f"[INSTALL] starting install: {command_name} from {github_repo_url} tag={git_tag}", flush=True)
    install_state: dict[str, Any] = {}
    success = False
    error_msg: str | None = None
    details: dict[str, Any] | None = None

    try:
        from services.command_store_service import install_from_github

        manifest = install_from_github(github_repo_url, version_tag=git_tag, state=install_state)
        success = True
        details = {
            "package_name": manifest.name,
            "version": manifest.version,
            "components": len(manifest.components),
        }
        print(f"[INSTALL] success: {manifest.name} v{manifest.version}", flush=True)
        logger.info(
            "Package installed successfully",
            request_id=request_id[:8],
            package=manifest.name,
            version=manifest.version,
        )
    except Exception as e:
        import traceback
        print(f"[INSTALL] FAILED: {e}", flush=True)
        traceback.print_exc()
        logger.error(
            "Package install failed",
            request_id=request_id[:8],
            command_name=command_name,
            error=str(e),
        )
        error_msg = str(e)

    pip_deps_installed: list[str] = install_state.get("pip_deps_installed", [])

    if pip_deps_installed:
        # Stale imports — new top-level modules won't be picked up by this
        # process. Defer the result POST and self-exit so systemd respawns
        # us; main.py on boot will flush the marker before starting voice.
        _defer_result_and_restart(
            request_id=request_id,
            success=success,
            error=error_msg,
            details=details,
            pip_deps_installed=pip_deps_installed,
        )
        # _defer_result_and_restart does not return.
        return

    # No new pip deps — refresh discovery in-process and upload the result.
    if success:
        _refresh_discovery_after_install(command_name)

    _upload_result(request_id, success=success, error=error_msg, details=details)


def _refresh_discovery_after_install(command_name: str) -> None:
    """Refresh in-process caches so a newly installed component is usable.

    Skipped when the install triggers a restart — boot does full discovery.

    The four discovery services are independent (different modules, different
    target dirs), so we refresh them in parallel. On Pi Zero this is the bulk
    of the post-install wait when there are no new pip deps to trigger a
    restart — each one invalidates Python's import caches and re-imports a
    pile of modules.
    """
    from concurrent.futures import ThreadPoolExecutor

    added_agents: set[str] = set()

    def _refresh_commands() -> None:
        from utils.command_discovery_service import get_command_discovery_service
        get_command_discovery_service().refresh_now()
        discovered = get_command_discovery_service().get_all_commands(include_disabled=True)
        if command_name in discovered:
            logger.info("Verified: command discoverable after install", command=command_name)
        else:
            logger.warning(
                "Command NOT found after refresh",
                command=command_name,
                discovered_count=len(discovered),
            )

    def _refresh_agents() -> None:
        nonlocal added_agents
        from utils.agent_discovery_service import get_agent_discovery_service

        discovery = get_agent_discovery_service()
        old_agents = set(discovery.get_all_agents().keys())
        discovery.refresh()
        added_agents = set(discovery.get_all_agents().keys()) - old_agents

    def _refresh_device_families() -> None:
        from utils.device_family_discovery_service import get_device_family_discovery_service
        dfd = get_device_family_discovery_service()
        dfd.refresh()
        logger.info(
            "Device family cache refreshed after install",
            families=sorted(dfd.get_all_families().keys()),
        )

    def _refresh_device_managers() -> None:
        from utils.device_manager_discovery_service import get_device_manager_discovery_service
        dmd = get_device_manager_discovery_service()
        dmd.refresh()
        logger.info(
            "Device manager cache refreshed after install",
            managers=sorted(dmd.get_all_managers().keys()),
        )

    refresh_jobs: dict[str, Any] = {
        "commands": _refresh_commands,
        "agents": _refresh_agents,
        "device_families": _refresh_device_families,
        "device_managers": _refresh_device_managers,
    }

    with ThreadPoolExecutor(max_workers=len(refresh_jobs), thread_name_prefix="pkg-discovery") as pool:
        futures = {pool.submit(fn): name for name, fn in refresh_jobs.items()}
        for fut, name in futures.items():
            try:
                fut.result()
            except Exception as e:
                logger.warning(
                    f"{name} discovery refresh after install failed (non-fatal)",
                    error=str(e),
                )

    # Agent scheduler update + run_agent_now MUST happen after agent
    # discovery — keep it serial here. run_agent_now is non-blocking
    # (asyncio.run_coroutine_threadsafe) so it doesn't add latency.
    if added_agents:
        try:
            from services.agent_scheduler_service import get_agent_scheduler_service
            from utils.agent_discovery_service import get_agent_discovery_service

            scheduler = get_agent_scheduler_service()
            scheduler.update_agents(get_agent_discovery_service().get_all_agents())
            for name in added_agents:
                logger.info("Running newly installed agent", agent=name)
                scheduler.run_agent_now(name)
        except Exception as e:
            logger.warning("Agent scheduler update after install failed (non-fatal)", error=str(e))


def _defer_result_and_restart(
    *,
    request_id: str,
    success: bool,
    error: str | None,
    details: dict[str, Any] | None,
    pip_deps_installed: list[str],
) -> None:
    """Persist the install result to a marker file and exit so systemd respawns.

    Does NOT return — calls ``os._exit(1)`` after writing the marker.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "success": success,
        "scheduled_at": time.time(),
        "pip_deps_installed": pip_deps_installed,
    }
    if error:
        payload["error"] = error
    if details:
        payload["details"] = details

    try:
        _PENDING_RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = _PENDING_RESULT_FILE.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload))
        # fsync + atomic rename so the marker survives the imminent exit
        with tmp_path.open("rb") as f:
            os.fsync(f.fileno())
        tmp_path.replace(_PENDING_RESULT_FILE)
    except Exception as e:
        # If we can't persist the marker, fall back to uploading the result
        # directly — at least the user sees an outcome. The stale-import
        # bug remains but that's better than mobile spinning forever.
        logger.error("Failed to write deferred install result; uploading directly", error=str(e))
        _upload_result(request_id, success=success, error=error, details=details)
        return

    logger.info(
        "Restarting node to pick up new pip imports — install result deferred",
        request_id=request_id[:8],
        pip_deps_installed=pip_deps_installed,
    )
    print(f"[INSTALL] restarting to load new pip deps: {pip_deps_installed}", flush=True)
    # Flush stdio so the log line lands before systemd respawns us.
    sys.stdout.flush()
    sys.stderr.flush()
    # Non-zero exit triggers Restart=on-failure in the systemd unit.
    os._exit(1)


def has_pending_install_result() -> bool:
    """Return True if a previous process deferred an install result.

    main.py uses this to suppress noisy boot behavior (LLM warmup TTS,
    etc.) on install-triggered restarts — the user already knows the
    node is alive because they just installed a package.
    """
    return _PENDING_RESULT_FILE.exists()


def flush_post_restart_install_result() -> None:
    """Post any install result deferred by the previous process, then clear it.

    Called from main.py during boot, after MQTT/LLM warmup are ready and
    before the voice listener starts. This guarantees:
    1. Mobile only sees the install as "Done" once the new process is
       fully usable (fresh imports + connected MQTT).
    2. Wake-word detection stays off during the install→restart window
       (voice listener hasn't started yet).
    """
    if not _PENDING_RESULT_FILE.exists():
        return

    try:
        payload = json.loads(_PENDING_RESULT_FILE.read_text())
    except Exception as e:
        logger.error("Pending install result is unreadable; clearing", error=str(e))
        try:
            _PENDING_RESULT_FILE.unlink()
        except OSError:
            pass
        return

    request_id = payload.get("request_id", "")
    success = bool(payload.get("success", False))
    error = payload.get("error")
    details = payload.get("details")

    logger.info(
        "Flushing deferred install result after restart",
        request_id=str(request_id)[:8],
        success=success,
        pip_deps_installed=payload.get("pip_deps_installed", []),
    )

    _upload_result(request_id, success=success, error=error, details=details)

    try:
        _PENDING_RESULT_FILE.unlink()
    except OSError as e:
        logger.warning("Failed to delete pending install marker", error=str(e))


def run_uninstall_and_upload(
    request_id: str,
    command_name: str,
    component_type: str | None = None,
) -> None:
    """Run package uninstall and upload results to CC. Meant to run in a background thread."""
    print(f"[UNINSTALL] starting uninstall: {command_name} (type={component_type})", flush=True)
    try:
        from services.command_store_service import remove

        remove(command_name, component_type=component_type)
        print(f"[UNINSTALL] success: {command_name}", flush=True)
        logger.info(
            "Package uninstalled successfully",
            request_id=request_id[:8],
            package=command_name,
        )

        _upload_result(request_id, success=True, details={
            "package_name": command_name,
        }, action="uninstall")
    except Exception as e:
        import traceback
        print(f"[UNINSTALL] FAILED: {e}", flush=True)
        traceback.print_exc()
        logger.error(
            "Package uninstall failed",
            request_id=request_id[:8],
            command_name=command_name,
            error=str(e),
        )
        _upload_result(request_id, success=False, error=str(e), action="uninstall")


def _upload_result(
    request_id: str,
    success: bool,
    error: str | None = None,
    details: dict[str, Any] | None = None,
    action: str = "install",
) -> None:
    """POST install/uninstall result to CC."""
    cc_url = get_command_center_url()
    if not cc_url:
        logger.error("Cannot upload result: CC URL not resolved")
        return

    from utils.config_service import Config
    node_id: str = Config.get_str("node_id", "") or ""

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/package-{action}/{request_id}/results"

    payload: dict[str, Any] = {"success": success}
    if error:
        payload["error"] = error
    if details:
        payload["details"] = details

    result = RestClient.post(url, data=payload, timeout=15)
    if result is not None:
        logger.info("Install result uploaded to CC", request_id=request_id[:8], success=success)
    else:
        logger.error("Failed to upload install result to CC", request_id=request_id[:8])
