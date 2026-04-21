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
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from jarvis_log_client import JarvisLogger

from core.runtime_state import is_busy
from core.version import version_info


logger = JarvisLogger(service="jarvis-node")


# State file lets the post-upgrade boot confirm what was attempted. Kept
# outside /opt/jarvis-node because install.sh backs that directory up to
# .bak during the upgrade — we'd lose the file.
STATE_FILE = Path("/var/lib/jarvis-node/update-state.json")

# Once an upgrade is in flight we ignore further pending_update entries
# until the process restarts. Prevents re-triggering if the heartbeat
# loop runs one more time before systemd tears us down.
_in_flight = threading.Event()


def _write_state(payload: dict[str, Any]) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except OSError as e:
        logger.error("Could not write update-state.json", error=str(e))


def read_state() -> dict[str, Any] | None:
    """Reads the state file if present. Used after restart."""
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def clear_state() -> None:
    try:
        STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning("Could not clear update-state.json", error=str(e))


def _spawn_upgrade(target_version: str) -> None:
    """Fork a detached installer as its own systemd unit.

    Running the installer via `systemd-run --unit=jarvis-node-update` places
    it in a separate cgroup from jarvis-node.service. Under cgroups v2 (Pi OS
    Bookworm and later) child processes inherit their parent's cgroup — so
    the previous `setsid` approach wasn't enough:
      - install.sh was subject to jarvis-node's memory pressure, so the OOM
        killer could (and did, on a 512 MB Pi Zero 2W) take out the installer
        mid-run.
      - install.sh couldn't safely `systemctl stop jarvis-node` to free RAM
        because that would tear down its own cgroup.

    With a dedicated transient unit both problems go away: the installer owns
    its own memory accounting and can stop/start jarvis-node freely. Output
    is appended to a log the Pi owner can tail if an upgrade gets stuck.

    `--collect` GCs the unit once the command exits. `--no-block` returns
    immediately so the caller (this Python service) can exit without waiting.
    `--same-dir` preserves CWD so relative paths in install.sh keep working.

    Falls back to the legacy `setsid` launch if `systemd-run` isn't on PATH
    (very old systems) — loses the cgroup isolation but preserves the
    existing behavior.
    """
    log_path = "/var/log/jarvis-node-update.log"
    # Pull install.sh from the TARGET version tag, not main. Curling from
    # main is dangerous — a broken push would brick every node that updates.
    cmd = (
        "curl -fsSL https://raw.githubusercontent.com/alexberardi/"
        f"jarvis-node-setup/v{target_version}/install.sh "
        f"| bash -s -- --force --version v{target_version}"
    )
    wrapped = f"({cmd}) >>{log_path} 2>&1"

    if shutil.which("systemd-run"):
        popen_args = [
            "systemd-run",
            "--unit=jarvis-node-update",
            "--collect",
            "--no-block",
            "--same-dir",
            "bash", "-c", wrapped,
        ]
    else:
        logger.warning(
            "systemd-run not found — falling back to setsid; installer will "
            "share jarvis-node's cgroup and may OOM on low-memory devices"
        )
        popen_args = ["setsid", "bash", "-c", wrapped]

    subprocess.Popen(
        popen_args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
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
    _spawn_upgrade(target_version)
