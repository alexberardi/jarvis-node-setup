"""Background watchdog that exits the process when command-center is
unreachable for too long, so systemd's ``Restart=always`` brings us back.

Why this exists
---------------
paho-mqtt's ``loop_forever()`` reconnect path keeps the process alive
even when the WiFi link is gone — the process never exits, systemd
never restarts us, and the node sits dead until someone power-cycles
it. This was the May-2026 beta blocker: prod node disconnected
overnight and only a forced reboot recovered it.

The watchdog probes a lightweight endpoint on the command-center
periodically and exits the process when N consecutive probes fail
(default: 5 failures over ~5 minutes). Exit code 0 — combined with
``Restart=always`` in the systemd unit, fresh process boots get a fresh
chance to re-associate with WiFi (handled by NetworkManager /
wpa_supplicant in userspace).

The watchdog is intentionally simple — no WiFi re-association attempt
from inside the process (we're not running as root and don't want to
fight NetworkManager). systemd restart is the recovery mechanism.

Tunables (config.json keys, all optional):
    connectivity_watchdog_enabled       bool, default True
    connectivity_watchdog_probe_seconds int,  default 60
    connectivity_watchdog_max_failures  int,  default 5
    connectivity_watchdog_timeout_secs  float, default 5.0
"""

from __future__ import annotations

import os
import threading
from typing import Optional

import httpx
from jarvis_log_client import JarvisLogger

from utils.config_service import Config

logger = JarvisLogger(service="jarvis-node")


def _command_center_url() -> Optional[str]:
    """Return the CC URL from config.json or env, normalised (no trailing slash)."""
    url = (
        Config.get_str("jarvis_command_center_api_url")
        or Config.get_str("command_center_url")
        or os.environ.get("JARVIS_CC_URL")
        or ""
    )
    return url.rstrip("/") or None


def _probe(url: str, timeout: float) -> bool:
    """Single HTTP HEAD-style probe. Any 2xx/3xx counts as reachable.

    Uses ``/api/v0/health`` (already in use by the rest of the codebase
    — see CLAUDE.md). 401/403 still count as reachable since they prove
    we got an HTTP response — the watchdog is checking *network*, not
    *auth*. Any exception (timeout, connect refused, DNS failure, SSL
    error) counts as unreachable.
    """
    try:
        r = httpx.get(f"{url}/api/v0/health", timeout=timeout)
        return r.status_code < 500
    except Exception:
        return False


def _watchdog_loop(
    probe_seconds: int,
    max_failures: int,
    timeout_secs: float,
    shutdown_event: threading.Event,
) -> None:
    consecutive_failures = 0
    while not shutdown_event.is_set():
        # Wait first so the watchdog doesn't fire during boot before
        # MQTT / WiFi have a chance to come up. Also doubles as the
        # inter-probe sleep on every subsequent iteration.
        if shutdown_event.wait(timeout=probe_seconds):
            return

        url = _command_center_url()
        if not url:
            # No CC configured (pre-provisioning). Don't penalise — the
            # node is still useful for setup flows.
            consecutive_failures = 0
            continue

        if _probe(url, timeout_secs):
            if consecutive_failures > 0:
                logger.info(
                    "Connectivity restored",
                    previous_consecutive_failures=consecutive_failures,
                )
            consecutive_failures = 0
            continue

        consecutive_failures += 1
        logger.warning(
            "Connectivity probe failed",
            url=url,
            consecutive_failures=consecutive_failures,
            max_failures=max_failures,
        )

        if consecutive_failures >= max_failures:
            logger.error(
                "Command-center unreachable for too long, exiting for systemd to restart",
                consecutive_failures=consecutive_failures,
                probe_seconds=probe_seconds,
                downtime_seconds=consecutive_failures * probe_seconds,
            )
            # os._exit, not sys.exit — sys.exit raises SystemExit, which
            # daemon threads can't propagate to the main thread. We want
            # the whole process to die immediately so systemd can restart
            # us with a fresh WiFi association attempt.
            os._exit(0)


def start_connectivity_watchdog(
    shutdown_event: threading.Event,
) -> Optional[threading.Thread]:
    """Start the watchdog as a daemon thread. Returns None when disabled."""
    if not Config.get_bool("connectivity_watchdog_enabled", True):
        logger.info("Connectivity watchdog disabled by config")
        return None

    probe_seconds = max(15, Config.get_int("connectivity_watchdog_probe_seconds", 60))
    max_failures = max(2, Config.get_int("connectivity_watchdog_max_failures", 5))
    timeout_secs = max(1.0, Config.get_float("connectivity_watchdog_timeout_secs", 5.0))

    logger.info(
        "Connectivity watchdog starting",
        probe_seconds=probe_seconds,
        max_failures=max_failures,
        timeout_secs=timeout_secs,
        downtime_threshold_seconds=probe_seconds * max_failures,
    )

    t = threading.Thread(
        target=_watchdog_loop,
        args=(probe_seconds, max_failures, timeout_secs, shutdown_event),
        daemon=True,
        name="connectivity-watchdog",
    )
    t.start()
    return t
