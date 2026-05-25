"""Background reachability watcher used during AP-mode provisioning.

When ``is_provisioned()`` returns False at boot — the marker file exists
but the saved command-center URL is unreachable — the node drops into
AP mode to await re-provisioning. That behavior is correct when the
home WiFi has *actually* changed (different network, password
rotated, etc.) and the user genuinely needs to set it up again.

It's WRONG when CC was merely transiently unreachable: the node tears
down the WiFi client to broadcast ``jarvis-XXXX`` and just sits there
forever, even after the home WiFi / CC return to normal. The user has
to physically reboot to get back to a working state. That was the
"overnight WiFi disconnect" symptom that dogged the May-2026 beta.

This watcher provides the self-healing path: while AP mode is active,
a daemon thread polls the saved CC URL every ``probe_seconds`` (default
30). When CC becomes reachable for ``consecutive_required`` probes in a
row (default 3 — ~90 s of stable reachability), the watcher calls
``sudo reboot``. The next boot re-runs ``is_provisioned()``, which now
succeeds, and the node returns to normal operation without any human
in the loop.

If CC stays unreachable (real WiFi change), the watcher does nothing
and the node remains in AP mode for the user to re-provision via the
mobile app — preserving the legitimate behavior the watchdog
fundamentally relies on.

Distinct from (and replacing) the v0.1.64 connectivity_watchdog, which
ran in the *main* service path and forced restart-loops that THEN
caused entry into AP mode. This watcher only runs *inside* AP mode and
provides a graceful exit back to normal.

Knobs (env vars; the watcher runs in AP mode where the Config service
may not be available, so env is the simplest source):

* ``JARVIS_RECOVERY_PROBE_SECONDS``   default 30
* ``JARVIS_RECOVERY_CONSECUTIVE``     default 3
* ``JARVIS_RECOVERY_PROBE_TIMEOUT``   default 5.0
* ``JARVIS_RECOVERY_DISABLED``        any non-empty value disables the watcher
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
from typing import Optional

import httpx
from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


def _read_saved_cc_url() -> Optional[str]:
    """Pull ``jarvis_command_center_api_url`` from config.json.

    Returns None when the file is unreadable or the key is missing —
    in which case there's nothing to watch and the recovery thread
    should not start.
    """
    path = os.environ.get("CONFIG_PATH", "/opt/jarvis-node/config.json")
    try:
        with open(path) as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return None
    url = cfg.get("jarvis_command_center_api_url")
    if isinstance(url, str) and url.strip():
        return url.rstrip("/")
    return None


def _probe(url: str, timeout: float) -> bool:
    """Single HTTP probe to ``{url}/api/v0/health``. Any 2xx/3xx/4xx
    response counts as reachable (we only care that the host is up and
    answering at the TCP/HTTP level). Exceptions = unreachable.
    """
    try:
        r = httpx.get(f"{url}/api/v0/health", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def _watcher_loop(
    url: str,
    *,
    probe_seconds: int,
    consecutive_required: int,
    probe_timeout: float,
    shutdown_event: threading.Event,
) -> None:
    consecutive = 0
    logger.info(
        "AP-mode recovery watcher started",
        url=url,
        probe_seconds=probe_seconds,
        consecutive_required=consecutive_required,
    )
    while not shutdown_event.is_set():
        # Wait first so we don't probe immediately after entering AP
        # mode (the network state machine is still settling — no point
        # asking).
        if shutdown_event.wait(timeout=probe_seconds):
            break
        if _probe(url, probe_timeout):
            consecutive += 1
            logger.info(
                "Saved CC URL reachable from AP mode",
                consecutive=consecutive,
                required=consecutive_required,
            )
            if consecutive >= consecutive_required:
                logger.warning(
                    "CC reachable for required consecutive probes — rebooting to exit AP mode",
                    url=url,
                )
                try:
                    subprocess.run(["sudo", "reboot"], check=False, timeout=10)
                except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
                    logger.error("sudo reboot failed", error=str(e))
                # If reboot succeeded the process is going down anyway.
                # If it didn't (PATH issue, permissions race), reset the
                # counter so we don't spam reboot attempts every probe.
                consecutive = 0
        else:
            if consecutive > 0:
                logger.info("Saved CC URL unreachable again — resetting recovery counter")
            consecutive = 0


def start_recovery_watcher(
    shutdown_event: threading.Event,
) -> Optional[threading.Thread]:
    """Spawn the AP-mode recovery watcher as a daemon thread.

    Returns the Thread on success, None when there's no saved CC URL
    (nothing meaningful to watch), or when the feature has been
    explicitly disabled via ``JARVIS_RECOVERY_DISABLED``.
    """
    if os.environ.get("JARVIS_RECOVERY_DISABLED"):
        logger.info("Recovery watcher disabled via JARVIS_RECOVERY_DISABLED")
        return None

    url = _read_saved_cc_url()
    if not url:
        logger.info(
            "No saved jarvis_command_center_api_url in config.json — "
            "AP-mode recovery watcher will not start"
        )
        return None

    probe_seconds = max(5, int(os.environ.get("JARVIS_RECOVERY_PROBE_SECONDS", "30")))
    consecutive = max(1, int(os.environ.get("JARVIS_RECOVERY_CONSECUTIVE", "3")))
    probe_timeout = max(1.0, float(os.environ.get("JARVIS_RECOVERY_PROBE_TIMEOUT", "5.0")))

    t = threading.Thread(
        target=_watcher_loop,
        args=(url,),
        kwargs=dict(
            probe_seconds=probe_seconds,
            consecutive_required=consecutive,
            probe_timeout=probe_timeout,
            shutdown_event=shutdown_event,
        ),
        daemon=True,
        name="ap-mode-recovery-watcher",
    )
    t.start()
    return t
