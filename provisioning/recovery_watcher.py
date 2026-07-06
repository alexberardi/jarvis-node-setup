"""AP-mode recovery: alternate between the provisioning AP and STA retries.

Without this module, AP mode is a one-way door: entering it tears down
the WiFi client, and the AP's captive dnsmasq answers every hostname
with the node's own address (192.168.4.1), so NO probe made from inside
AP mode can ever observe the home network recovering. The previous
(v0.1.69) recovery watcher polled the saved command-center URL through
that captive DNS and therefore could never fire — which is how the prod
kitchen node ended up stranded in AP mode on 2026-07-05 until someone
physically pulled the plug.

This watcher makes AP mode recoverable by physically alternating modes:

  1. Broadcast the provisioning AP for ``retry_interval`` (default 600s)
     so a user who genuinely changed their WiFi can pair — the
     legitimate reason AP mode exists.
  2. Then, unless a pairing session is actively in progress, tear the AP
     down and let the WiFi client retry the KNOWN network for
     ``sta_window`` (default 120s). Both WiFi backends restore the
     client stack in ``stop_ap_mode`` (NetworkManager autoconnects to
     the priority-999 profile installed at provisioning time).
  3. Joined → reboot: the provisioning marker plus working WiFi means
     the next boot proceeds straight to normal operation.
  4. Not joined → restore the AP and repeat forever.

Net effect: a transient outage that pushed a provisioned node into AP
mode self-heals within ~one retry interval with no human involved, while
a genuine WiFi change keeps the AP available for re-provisioning most of
the time (AP downtime is bounded by ``sta_window``).

Knobs (env vars — Config service isn't available in AP mode):

* ``JARVIS_RECOVERY_RETRY_INTERVAL``  seconds of AP time between STA retries (default 600)
* ``JARVIS_RECOVERY_STA_WINDOW``      seconds to wait for the known WiFi to join (default 120)
* ``JARVIS_RECOVERY_POLL_SECONDS``    join-poll cadence inside the STA window (default 5)
* ``JARVIS_RECOVERY_DISABLED``        any non-empty value disables the watcher
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from typing import Callable, Optional

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")


def _default_reboot() -> None:
    try:
        subprocess.run(["sudo", "reboot"], check=False, timeout=10)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.error("sudo reboot failed", error=str(e))


def _watcher_loop(
    *,
    wifi_manager,
    ap_ssid: str,
    is_pairing_active: Callable[[], bool],
    shutdown_event: threading.Event,
    retry_interval: float,
    sta_window: float,
    poll_seconds: float,
    reboot_fn: Callable[[], None] = _default_reboot,
) -> None:
    logger.info(
        "AP/STA recovery cycle started",
        ap_ssid=ap_ssid,
        retry_interval=retry_interval,
        sta_window=sta_window,
    )
    while not shutdown_event.is_set():
        # AP phase: stay available for pairing.
        if shutdown_event.wait(timeout=retry_interval):
            break

        if is_pairing_active():
            # Someone is mid-provisioning — never yank the AP away.
            logger.info("STA retry skipped — pairing session active")
            continue

        logger.info(
            "Attempting STA recovery — dropping AP to retry known WiFi",
            sta_window=sta_window,
        )
        try:
            wifi_manager.stop_ap_mode()
        except Exception as e:
            logger.error("stop_ap_mode failed during STA retry", error=str(e))
            continue

        joined = False
        deadline = time.monotonic() + sta_window
        while not shutdown_event.is_set() and time.monotonic() < deadline:
            try:
                if wifi_manager.get_current_ssid():
                    joined = True
                    break
            except Exception:
                pass
            if shutdown_event.wait(timeout=poll_seconds):
                break

        if joined:
            logger.warning(
                "Known WiFi rejoined during STA retry — rebooting to resume normal operation",
            )
            reboot_fn()
            return

        logger.info("Known WiFi still unavailable — restoring provisioning AP")
        try:
            wifi_manager.start_ap_mode(ap_ssid)
        except Exception as e:
            logger.error("start_ap_mode failed after STA retry", error=str(e))


def start_recovery_watcher(
    shutdown_event: threading.Event,
    *,
    wifi_manager,
    ap_ssid: str,
    is_pairing_active: Callable[[], bool],
) -> Optional[threading.Thread]:
    """Spawn the AP↔STA recovery cycle as a daemon thread.

    Only meaningful for a node that has been provisioned before (there is
    a known WiFi to retry). Callers gate on the provisioning marker; a
    fresh node stays in plain AP mode until first pairing.

    Returns the Thread, or None when disabled via
    ``JARVIS_RECOVERY_DISABLED``.
    """
    if os.environ.get("JARVIS_RECOVERY_DISABLED"):
        logger.info("Recovery watcher disabled via JARVIS_RECOVERY_DISABLED")
        return None

    retry_interval = max(30.0, float(os.environ.get("JARVIS_RECOVERY_RETRY_INTERVAL", "600")))
    sta_window = max(15.0, float(os.environ.get("JARVIS_RECOVERY_STA_WINDOW", "120")))
    poll_seconds = max(1.0, float(os.environ.get("JARVIS_RECOVERY_POLL_SECONDS", "5")))

    t = threading.Thread(
        target=_watcher_loop,
        kwargs=dict(
            wifi_manager=wifi_manager,
            ap_ssid=ap_ssid,
            is_pairing_active=is_pairing_active,
            shutdown_event=shutdown_event,
            retry_interval=retry_interval,
            sta_window=sta_window,
            poll_seconds=poll_seconds,
        ),
        daemon=True,
        name="ap-sta-recovery-cycle",
    )
    t.start()
    return t
