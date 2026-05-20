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

from core.runtime_state import is_busy
from core.version import version_info


logger = JarvisLogger(service="jarvis-node")


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


def maybe_apply_update(pending: dict[str, Any]) -> None:
    """Act on a `pending_update` block from the heartbeat response.

    Early-returns for:
    - An upgrade already in flight (idempotent re-dispatch)
    - Docker / dev installs (those update out-of-band)
    - A busy node (belt-and-suspenders; CC should already defer)
    - A task matching our current version (nothing to do)
    """
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

    if current.version == target_version:
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
