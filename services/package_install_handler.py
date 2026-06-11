"""Handle package install requests from CC via MQTT.

Flow:
1. CC publishes to jarvis/nodes/{node_id}/package-install with request_id + repo info
2. This handler runs install_from_github() from command_store_service
3. Results are POSTed back to CC for mobile to poll

Every successful install (and uninstall/revert) restarts the node service
instead of reloading in-process. In-process reload corrupted long-lived state in the
field — duplicate MQTT message handlers after re-registration, stale module
caches for new pip imports (a failed ``ImportError`` at boot caches ``None``
for the import target) — and restart makes correctness structural. We defer
the result POST to a marker file, best-effort POST a ``restarting`` status so
mobile can show progress, and self-exit non-zero so systemd respawns the
service. ``flush_post_restart_install_result()`` is called from main.py /
text_mode.py during boot, AFTER command + agent discovery have initialized,
to verify the installed components actually loaded and complete the
round-trip — that's also why mobile only sees "Done" once the new process is
fully ready, and why the wake-word path never engages during the restart
window (voice listener isn't started until after the flush).

Failures never restart: the pre-flight validation gate in
command_store_service means a failed install scattered nothing, so the
failure is POSTed immediately.
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
# to complete the install round-trip after a restart.
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
    error_msg: str | None = None
    details: dict[str, Any] | None = None
    manifest = None

    try:
        from services.command_store_service import install_from_github

        manifest = install_from_github(github_repo_url, version_tag=git_tag, state=install_state)
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

    if manifest is None:
        # FAILURE: the pre-flight gate means nothing was scattered, so the
        # running process is untouched — post immediately, never restart.
        # (Any pip deps the failed attempt installed are idempotent and
        # harmless.)
        _upload_result(request_id, success=False, error=error_msg, details=details)
        return

    # SUCCESS: ALWAYS restart. In-process reload corrupts long-lived state
    # (duplicate MQTT handlers, stale import caches); a fresh boot does full
    # discovery and the post-restart flush verifies the new components
    # actually loaded before reporting "done".
    _defer_result_and_restart(
        request_id=request_id,
        success=True,
        error=None,
        details=details,
        pip_deps_installed=install_state.get("pip_deps_installed", []),
        action="install",
        package_name=manifest.name,
    )
    # _defer_result_and_restart does not return.


def _defer_result_and_restart(
    *,
    request_id: str,
    success: bool,
    error: str | None,
    details: dict[str, Any] | None,
    pip_deps_installed: list[str],
    action: str = "install",
    package_name: str | None = None,
) -> None:
    """Persist the result to a marker file and exit so systemd respawns.

    Does NOT return — ``os._exit(1)`` is ALWAYS reached, even when the
    marker write fails: the stale-state bug (duplicate MQTT handlers,
    poisoned import caches) lives in THIS process, and only a fresh boot
    clears it. Normally we write the marker and best-effort POST a
    ``restarting`` status so CC/mobile can show "Restarting node…" instead
    of a silent gap until the post-boot flush. When the marker write fails,
    we upload the terminal result directly instead and SKIP the restarting
    POST — a restarting POST landing after a terminal result would strand
    CC in 'restarting'.
    """
    payload: dict[str, Any] = {
        "request_id": request_id,
        "success": success,
        "scheduled_at": time.time(),
        "pip_deps_installed": pip_deps_installed,
        "action": action,
        "package_name": package_name,
        # Boot-loop guard for the (Phase 4) auto-rollback: at most one
        # rollback attempt per request.
        "rollback_attempted": False,
    }
    if error:
        payload["error"] = error
    if details:
        payload["details"] = details

    try:
        _write_marker_file(payload)
    except Exception as e:
        # If we can't persist the marker, upload the terminal result
        # directly — at least the user sees an outcome — and skip the
        # restarting POST (it would arrive after the terminal result and
        # strand CC in 'restarting'). The restart below still happens:
        # skipping it would leave this process's corrupted state running.
        logger.error("Failed to write deferred install result; uploading directly", error=str(e))
        try:
            _upload_result(request_id, success=success, error=error, details=details, action=action)
        except Exception as upload_error:
            # Nothing more we can do — the restart must not be blocked.
            logger.error("Direct result upload also failed", error=str(upload_error))
    else:
        _post_restarting_status(request_id, action=action, details=details)

    logger.info(
        "Restarting node after package change — result deferred to next boot",
        request_id=request_id[:8],
        action=action,
        package=package_name,
        pip_deps_installed=pip_deps_installed,
    )
    print(f"[{action.upper()}] restarting node to apply package change", flush=True)
    # Flush stdio so the log line lands before systemd respawns us.
    sys.stdout.flush()
    sys.stderr.flush()
    # Non-zero exit triggers Restart=on-failure in the systemd unit.
    os._exit(1)


def _write_marker_file(payload: dict[str, Any]) -> None:
    """Atomically persist the deferred-result marker.

    fsync + atomic rename so the marker survives the imminent ``os._exit``.
    Raises on failure — callers decide the fallback.
    """
    _PENDING_RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = _PENDING_RESULT_FILE.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload))
    with tmp_path.open("rb") as f:
        os.fsync(f.fileno())
    tmp_path.replace(_PENDING_RESULT_FILE)


def _post_restarting_status(
    request_id: str,
    *,
    action: str,
    details: dict[str, Any] | None,
) -> None:
    """Best-effort 'restarting' status POST before the self-exit.

    Swallows every error — the marker file is the source of truth and the
    restart must never be blocked on CC reachability.
    """
    try:
        cc_url = get_command_center_url()
        if not cc_url:
            return

        from utils.config_service import Config
        node_id: str = Config.get_str("node_id", "") or ""

        url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/package-{action}/{request_id}/results"
        RestClient.post(
            url,
            data={"success": True, "restarting": True, "details": details or {}},
            timeout=3,
        )
    except Exception:
        pass


def has_pending_install_result() -> bool:
    """Return True if a previous process deferred an install result.

    main.py uses this to suppress noisy boot behavior (LLM warmup TTS,
    etc.) on install-triggered restarts — the user already knows the
    node is alive because they just installed a package.
    """
    return _PENDING_RESULT_FILE.exists()


def flush_post_restart_install_result() -> None:
    """Post any result deferred by the previous process, then clear it.

    Called from main.py / text_mode.py during boot, AFTER command + agent
    discovery have initialized and before the voice listener starts. This
    guarantees:
    1. Mobile only sees the install as "Done" once the new process is
       fully usable (fresh imports + the package's components provably
       discovered — see _verify_installed_components_loaded).
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
    # Default "install" keeps back-compat with a marker written by the
    # previous node version mid-upgrade.
    action = str(payload.get("action") or "install")
    package_name = payload.get("package_name") or (details or {}).get("package_name")

    if action == "install" and success and package_name:
        failed = _verify_installed_components_loaded(str(package_name))
        if failed:
            success = False
            error = "Installed but components failed to load: " + "; ".join(failed)
            if not payload.get("rollback_attempted"):
                # Auto-rollback (at most once per request — the marker's
                # rollback_attempted flag is the boot-loop guard): revert
                # to the .previous snapshot when one exists, rewrite the
                # marker, and restart once more so the restored version's
                # modules load fresh. Does not return when it succeeds.
                rollback_error = _attempt_auto_rollback(
                    payload, str(package_name), failed
                )
                if rollback_error:
                    error = rollback_error

    logger.info(
        "Flushing deferred package result after restart",
        request_id=str(request_id)[:8],
        action=action,
        success=success,
        pip_deps_installed=payload.get("pip_deps_installed", []),
    )

    _upload_result(request_id, success=success, error=error, details=details, action=action)

    try:
        _PENDING_RESULT_FILE.unlink()
    except OSError as e:
        logger.warning("Failed to delete pending install marker", error=str(e))


def _attempt_auto_rollback(
    payload: dict[str, Any],
    package_name: str,
    failures: list[str],
) -> str | None:
    """Auto-rollback a failed install to its .previous snapshot.

    On success this rewrites the marker with ``rollback_attempted=True``,
    ``success=False`` and the rolled-back error message, then ``os._exit(1)``s
    for ONE extra restart (does not return). The next boot's flush sees a
    failed marker, skips the health check entirely, and just posts the
    failure — the rewritten flag guarantees no rollback loop.

    Returns:
        None when rollback wasn't possible (no .previous snapshot, or the
        revert itself raised) — the caller posts the plain
        components-failed error. If the marker rewrite fails after a
        successful revert, returns the rolled-back error string so the
        caller posts it immediately instead of restarting.
    """
    try:
        from services.command_store_service import has_previous_version, revert_package

        if not has_previous_version(package_name):
            return None
        restored_version = revert_package(package_name)
    except Exception as e:
        logger.error("Auto-rollback failed", package=package_name, error=str(e))
        return None

    error = (
        f"Install failed to load — rolled back to {restored_version}. "
        + "; ".join(failures)
    )

    marker = dict(payload)
    marker["success"] = False
    marker["error"] = error
    marker["rollback_attempted"] = True

    try:
        _write_marker_file(marker)
    except Exception as e:
        logger.error(
            "Could not rewrite marker after rollback; posting result without restart",
            package=package_name,
            error=str(e),
        )
        return error

    # Best-effort 'restarting' status (errors swallowed inside) so CC grants
    # the rollback's second boot its own +120s expiry extension — CC accepts
    # repeat restarting posts; without this a slow second boot could expire
    # the request mid-restart.
    _post_restarting_status(
        str(payload.get("request_id", "")),
        action=str(payload.get("action") or "install"),
        details=payload.get("details"),
    )

    logger.info(
        "Rolled back failed install — restarting to load the previous version",
        package=package_name,
        restored_version=restored_version,
    )
    print(
        f"[INSTALL] rolled back {package_name} to {restored_version}; restarting",
        flush=True,
    )
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(1)


def _verify_installed_components_loaded(package_name: str) -> list[str]:
    """Verify the package's components actually loaded after the restart.

    Phase-2 stub: presence checks against command + agent discovery only
    (other component types pass). Phase 3 extends discovery with the
    unconfigured-agent and failed-module registries; those are already read
    here via getattr so this function works before and after they exist.

    A presence miss WITHOUT a recorded import error does NOT count as a
    failure: the manifest component name may legitimately differ from the
    runtime command_name/agent name, and rolling back a healthy install is
    worse than trusting it. Only a real failed-modules registry entry fails
    the health check.

    Returns:
        "name (reason)" strings for components that failed to load. Empty
        list means everything verifiable loaded.
    """
    try:
        from services.command_store_service import PACKAGES_DIR
        meta_path = PACKAGES_DIR / f"{package_name}.json"
        components = json.loads(meta_path.read_text()).get("components", [])
    except Exception as e:
        logger.warning(
            "Cannot verify install health — package metadata unreadable",
            package=package_name, error=str(e),
        )
        return []

    failures: list[str] = []
    for comp in components:
        comp_type = comp.get("type")
        comp_name = str(comp.get("name") or "")
        if not comp_name:
            continue
        try:
            if comp_type == "command":
                from utils.command_discovery_service import get_command_discovery_service

                svc = get_command_discovery_service()
                commands = svc.get_all_commands(include_disabled=True)
                if not commands:
                    # Discovery hasn't run in this process yet (boot paths
                    # initialize it lazily) — force one pass before judging.
                    svc.refresh_now()
                    commands = svc.get_all_commands(include_disabled=True)
                if comp_name not in commands:
                    reason = _failed_module_error(svc, comp_name)
                    if reason:
                        failures.append(f"{comp_name} ({reason})")
                    else:
                        logger.warning(
                            "Command not found by manifest name and no import "
                            "error recorded — treating as loaded — runtime "
                            "name may differ",
                            component=comp_name,
                        )
            elif comp_type == "agent":
                from utils.agent_discovery_service import get_agent_discovery_service

                svc = get_agent_discovery_service()
                if comp_name in svc.get_all_agents():
                    continue
                # Unconfigured (missing secrets) counts as loaded — the
                # user fixes that from the mobile card, not by reinstalling.
                get_unconfigured = getattr(svc, "get_unconfigured_agents", None)
                if callable(get_unconfigured) and comp_name in (get_unconfigured() or {}):
                    continue
                reason = _failed_module_error(svc, comp_name)
                if reason:
                    failures.append(f"{comp_name} ({reason})")
                else:
                    logger.warning(
                        "Agent not found by manifest name and no import "
                        "error recorded — treating as loaded — runtime "
                        "name may differ",
                        component=comp_name,
                    )
            # Other component types (device protocols/managers, routines)
            # are not presence-checked by this stub.
        except Exception as e:
            # Verification must never turn a good install into a reported
            # failure because discovery itself hiccuped.
            logger.warning(
                "Component load verification errored (treated as loaded)",
                component=comp_name, error=str(e),
            )
    return failures


def _failed_module_error(discovery_service: Any, name: str) -> str | None:
    """Read the (Phase 3) failed-module registry if the service has one."""
    get_failed = getattr(discovery_service, "get_failed_modules", None)
    if not callable(get_failed):
        return None
    try:
        failed = get_failed() or {}
    except Exception:
        return None
    err = failed.get(name)
    return str(err) if err else None


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
        return

    # SUCCESS: restart — in-process unload is as unsafe as in-process
    # reload (stale modules keep the removed code alive until a restart
    # anyway, and half-unloaded handlers corrupt the MQTT runtime).
    _defer_result_and_restart(
        request_id=request_id,
        success=True,
        error=None,
        details={"package_name": command_name},
        pip_deps_installed=[],
        action="uninstall",
        package_name=command_name,
    )
    # _defer_result_and_restart does not return.


def run_revert_and_upload(request_id: str, command_name: str) -> None:
    """Run package revert and upload results to CC. Meant to run in a background thread."""
    print(f"[REVERT] starting revert: {command_name}", flush=True)
    try:
        from services.command_store_service import revert_package

        restored_version = revert_package(command_name)
        print(f"[REVERT] success: {command_name} -> v{restored_version}", flush=True)
        logger.info(
            "Package reverted successfully",
            request_id=request_id[:8],
            package=command_name,
            version=restored_version,
        )
    except Exception as e:
        import traceback
        print(f"[REVERT] FAILED: {e}", flush=True)
        traceback.print_exc()
        logger.error(
            "Package revert failed",
            request_id=request_id[:8],
            command_name=command_name,
            error=str(e),
        )
        _upload_result(request_id, success=False, error=str(e), action="revert")
        return

    # SUCCESS: restart — same reasoning as install/uninstall: the restored
    # version's modules must be loaded by a fresh process, and the
    # post-restart flush completes the round-trip to CC.
    _defer_result_and_restart(
        request_id=request_id,
        success=True,
        error=None,
        details={"package_name": command_name, "version": restored_version},
        pip_deps_installed=[],
        action="revert",
        package_name=command_name,
    )
    # _defer_result_and_restart does not return.


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
