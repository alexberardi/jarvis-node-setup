"""Node-side update handler.

The CC heartbeat response may include a `pending_update` block:

    {"pending_update": {"task_id": "...", "target_version": "0.3.0"}}

When that arrives, `maybe_apply_update()` forks a detached upgrade shell
(so it survives when systemd kills the current node process) and writes
a state file so the restarted node can tell CC what happened.

Only tarball installs are supported. Docker and dev modes short-circuit;
the user should update those manually. The state machine is simple:

    pending_update received → write state file → fork detached installer
        → systemd stops us → installer rewrites /opt/jarvis-node → installer
        restarts jarvis-node.service → we boot back up with new VERSION →
        next heartbeat reports the new version → CC reconciles.

This module doesn't need to notify CC directly; the post-upgrade version
in the heartbeat payload is the success signal.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from core.runtime_state import is_busy
from core.version import version_info
from utils.config_service import Config
from utils.service_discovery import get_command_center_url


logger = JarvisLogger(service="jarvis-node")


# Actionable for every consumer class: new mobile builds have the Enable
# updates control; older builds / phones without this node's K2 key get the
# config.json pointer instead of a dead end.
_POLICY_REFUSAL_MESSAGE = (
    "Updates are disabled on this node (allow_updates is off). "
    "Enable updates from the node's screen in the Jarvis app, or set "
    '"allow_updates": true in the node\'s config.json, then retry.'
)


def _report_task_refused(task_id: str) -> None:
    """Best-effort: mark the update task failed on CC with the policy reason.

    Without this the task dies as a sweeper timeout ~15 minutes later and the
    operator sees "Timeout: no heartbeat..." instead of the actual reason.
    Never raises — the caller runs inside the heartbeat loop.
    """
    try:
        base_url = get_command_center_url() or ""
        if not base_url:
            logger.warning(
                "Cannot report update refusal: no command-center URL",
                task_id=task_id,
            )
            return
        url = f"{base_url.rstrip('/')}/api/v0/nodes/tasks/{task_id}/status"
        result = RestClient.post(
            url,
            data={"state": "failed", "error_message": _POLICY_REFUSAL_MESSAGE},
            timeout=10,
        )
        if result is None:
            logger.warning("Update refusal report failed", task_id=task_id)
    except Exception as e:
        logger.warning(
            "Update refusal report errored", task_id=task_id, error=str(e)
        )


def _state_file() -> Path:
    """Resolve the post-upgrade state file path.

    Lives under the service user's secret dir (~/.jarvis/state/) rather
    than /var/lib/jarvis-node — the latter is root-owned, and the
    service runs as a non-root user post-migration. Honors
    JARVIS_SECRET_DIRECTORY for parity with the rest of the secret
    layout. Kept outside /opt/jarvis-node because install.sh moves
    that directory aside during upgrades.
    """
    secret_dir = Path(os.environ.get("JARVIS_SECRET_DIRECTORY",
                                     str(Path.home() / ".jarvis")))
    return secret_dir / "state" / "update-state.json"


# Once an upgrade is in flight we ignore further pending_update entries
# until the process restarts. Prevents re-triggering if the heartbeat
# loop runs one more time before systemd tears us down.
_in_flight = threading.Event()


def _write_state(payload: dict[str, Any]) -> None:
    state_file = _state_file()
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        logger.error("Could not write update-state.json", error=str(e))


def read_state() -> dict[str, Any] | None:
    """Reads the state file if present. Used after restart."""
    try:
        return json.loads(_state_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_state() -> None:
    try:
        _state_file().unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not clear update-state.json", error=str(e))


_SELF_UPDATE_WRAPPER = "/usr/local/sbin/jarvis-self-update"


class UpgradeSpawnError(Exception):
    """Spawning the upgrade installer failed (sudo, missing wrapper, ...).

    Raised so the caller can clear ``_in_flight`` and surface the failure
    instead of leaving CC's task ``in_progress`` for 30 min until the
    server-side sweeper gives up.
    """


def _spawn_upgrade(target_version: str) -> None:
    """Hand off to the privileged self-update wrapper.

    ``/usr/local/sbin/jarvis-self-update`` is a tiny shell shim installed by
    install.sh with a NOPASSWD sudoers grant. It validates the version
    tag and runs install.sh inside a transient systemd unit, which:

      - owns its own cgroup (escapes jarvis-node.service's memory limit,
        avoiding OOM on 512 MB Pi Zero 2W)
      - can stop/start jarvis-node freely without tearing down its own
        cgroup
      - sends output to ``journalctl -u jarvis-node-update`` for tailing

    History: this used to call ``sudo systemd-run bash -c 'curl | bash'``
    directly. That command isn't in the NOPASSWD allow-list, so sudo
    prompted for a password — and since jarvis-node has no TTY, sudo
    failed silently with ``a password is required`` while the daemon
    fire-and-forgot the Popen. Result: prod kitchen node sat
    ``in_progress`` for 30 min on every update attempt before the
    server-side sweeper marked it failed. The wrapper + NOPASSWD grant
    fixes that, and ``subprocess.run`` (with a short timeout against
    the wrapper's ``systemd-run --no-block``, which returns in <1s)
    actually surfaces spawn failures instead of swallowing them.

    Raises:
        UpgradeSpawnError if sudo/the wrapper exits non-zero. Caller is
        responsible for clearing ``_in_flight`` and the state file.
    """
    version_tag = f"v{target_version}"
    cmd = ["sudo", "-n", _SELF_UPDATE_WRAPPER, version_tag]

    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=15,  # wrapper's systemd-run --no-block returns in <1s
            check=False,
        )
    except FileNotFoundError as e:
        raise UpgradeSpawnError(f"sudo not found: {e}") from e
    except subprocess.TimeoutExpired as e:
        raise UpgradeSpawnError(
            "jarvis-self-update did not return within 15s — wrapper hung?"
        ) from e

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        raise UpgradeSpawnError(
            f"jarvis-self-update exited {result.returncode}: {stderr or '<no stderr>'}"
        )


def _parse_version(v: str | None) -> tuple[int, ...] | None:
    """Parse a semver-ish ``X.Y.Z`` into a comparable tuple, or ``None`` if it
    isn't parseable. A leading ``v`` and any ``-suffix`` / ``+build`` are ignored,
    so ``v0.1.5``, ``0.1.5``, and ``0.1.5-dev`` all compare as ``(0, 1, 5)``.
    """
    if not v:
        return None
    core = v.strip().lstrip("v").split("-", 1)[0].split("+", 1)[0]
    try:
        return tuple(int(p) for p in core.split("."))
    except ValueError:
        return None


def maybe_apply_update(pending: dict[str, Any]) -> None:
    """Act on a `pending_update` block from the heartbeat response.

    Early-returns for:
    - An upgrade already in flight (idempotent re-dispatch)
    - Docker / dev installs (those update out-of-band)
    - A busy node (belt-and-suspenders; CC should already defer)
    - A task matching our current version (nothing to do)

    Gated by the ``allow_updates`` policy (env ``JARVIS_ALLOW_UPDATES``),
    which defaults to False. When disabled this short-circuits before any
    state write or installer spawn — no GitHub/wrapper egress at all.
    """
    if not Config.get_bool("allow_updates", False):
        logger.info(
            "Update requested but updates are disabled by policy — ignoring",
            task_id=pending.get("task_id"),
        )
        # Tell CC WHY, or the task dies ~15 minutes later as a misleading
        # sweeper timeout and the operator never learns the reason
        # (Jul-2026 prod: an update "failure" that was actually this gate).
        refused_task_id = pending.get("task_id")
        if refused_task_id:
            _report_task_refused(refused_task_id)
        return

    if _in_flight.is_set():
        return

    task_id = pending.get("task_id")
    target_version = pending.get("target_version")
    if not task_id or not target_version:
        logger.warning("pending_update missing task_id or target_version", payload=pending)
        return

    current = version_info()
    if current.install_mode != "tarball":
        logger.warning(
            "Update requested but install_mode is not tarball — ignoring",
            install_mode=current.install_mode,
            task_id=task_id,
        )
        return

    if is_busy():
        logger.info("Deferring update — node is busy", task_id=task_id)
        return

    # Monotonic guard: never downgrade or re-install. A replayed or compromised
    # pending_update must not be able to roll the node back to an older (possibly
    # vulnerable) version. Only a strictly-newer, parseable target proceeds; if
    # either version is unparseable, fall back to the equality no-op.
    cur = _parse_version(current.version)
    tgt = _parse_version(target_version)
    if cur is not None and tgt is not None:
        if tgt <= cur:
            logger.info(
                "Refusing update — target is not newer than current (no downgrade)",
                current=current.version,
                target=target_version,
                task_id=task_id,
            )
            return
    elif current.version == target_version:
        logger.info("Already at target version — nothing to do", version=current.version)
        return

    logger.info(
        "Applying update",
        task_id=task_id,
        from_version=current.version,
        to_version=target_version,
    )
    _in_flight.set()
    _write_state({
        "task_id": task_id,
        "target_version": target_version,
        "previous_version": current.version,
    })
    try:
        _spawn_upgrade(target_version)
    except UpgradeSpawnError as e:
        # Spawn failed before the installer ever started (sudo prompt,
        # missing wrapper, etc.). Roll back state immediately so the next
        # heartbeat from CC re-dispatches and so we can surface the real
        # error to the operator instead of a silent 30-min "in_progress"
        # window while the server-side sweeper notices the task is dead.
        logger.error(
            "Update spawn failed — rolling back in_flight state",
            task_id=task_id,
            target_version=target_version,
            error=str(e),
        )
        clear_state()
        _in_flight.clear()
        raise
