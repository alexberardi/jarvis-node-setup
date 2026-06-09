"""Daily self-restart for memory hygiene on long-running nodes.

On a Pi Zero 2W with 416 MB RAM, even a well-behaved Python service
accumulates a few MB/h of allocator fragmentation, library-internal
caches, and unreleased pymalloc arenas (CPython holds them for reuse
across the process lifetime, never returning them to the OS). Over a
week, that drifts into swap-thrash territory. A short daily restart
clears all of it cleanly — the kernel reclaims every page, the systemd
``Restart=always`` directive brings us right back up, and any in-flight
voice request retries naturally from the mobile/voice client.

Two independent triggers:

1. **Scheduled time** — at the configured ``maintenance_restart_at_time``
   (HH:MM in node-local time), exit. Default ``03:00``. Once-per-day guard
   so a clock anomaly can't fire twice in the same minute.

2. **RSS ceiling** — if the process's resident set crosses
   ``maintenance_restart_rss_ceiling_mb`` (default 320 MB), exit early.
   Belt-and-braces against a future regression that drives the leak rate
   above what the daily restart bounds. Logged loudly so we notice.

Both are gated by ``maintenance_restart_enabled``. Setting it to false
disables the whole subsystem at runtime (poll picks up the change on
the next 60 s iteration — no restart needed to disable).

The check loop polls every 60 s. ``os._exit(0)`` is used instead of
``sys.exit`` so cleanup hooks don't intercept it and turn the
3-second downtime into a 30-second TimeoutStopSec wait. atexit drains
the log queue via JarvisLogger.shutdown registered in __init__.
"""

from __future__ import annotations

import datetime
import os
import threading
import time
from pathlib import Path
from typing import Optional

from jarvis_log_client import JarvisLogger

from utils.config_service import Config

logger = JarvisLogger(service="jarvis-node")


# ── Silent-restart marker ────────────────────────────────────────────────
#
# Mirrors ``services.package_install_handler._PENDING_RESULT_FILE``. main.py
# reads this on boot to suppress the LLM-warmup TTS — a 3 AM scheduled
# restart that loudly said "Hello! How can I assist you today?" would
# defeat the entire purpose of running a quiet maintenance window. Same
# treatment for the RSS-ceiling emergency stop: the user didn't ask for
# the restart, doesn't need an audible "I'm back" confirmation.
#
# Marker lives in ``~/.jarvis/`` so it survives the ~3 s systemctl
# restart but gets cleared by ``flush_pending_maintenance_restart`` on
# the next boot. /tmp would NOT work — it's RAM-backed and we'd lose
# the signal across reboots that go through a full power cycle.
_MAINTENANCE_RESTART_MARKER = (
    Path.home() / ".jarvis" / ".pending_maintenance_restart"
)


def has_pending_maintenance_restart() -> bool:
    """True if the previous process exited via the maintenance scheduler.

    main.py reads this to suppress the LLM-warmup audible response on
    boot. Clear the marker via ``clear_pending_maintenance_restart``
    once the signal has been consumed.
    """
    return _MAINTENANCE_RESTART_MARKER.exists()


def clear_pending_maintenance_restart() -> None:
    """Remove the marker file. Idempotent — safe if it doesn't exist."""
    try:
        _MAINTENANCE_RESTART_MARKER.unlink(missing_ok=True)
    except OSError as e:
        logger.warning(
            "Failed to clear maintenance-restart marker",
            error=str(e),
            path=str(_MAINTENANCE_RESTART_MARKER),
        )


def _write_maintenance_restart_marker(reason: str) -> None:
    """Drop the marker file so the next boot stays silent."""
    try:
        _MAINTENANCE_RESTART_MARKER.parent.mkdir(parents=True, exist_ok=True)
        # Reason is informational only — caller never reads the body.
        _MAINTENANCE_RESTART_MARKER.write_text(
            f"{reason}\n{datetime.datetime.now().isoformat()}\n",
        )
    except OSError as e:
        # Best-effort: if we can't write the marker the worst case is a
        # spoken "Hello…" at 3 AM, which is the bug. Log loudly so the
        # operator can spot the next-day pattern in journalctl.
        logger.error(
            "Failed to write maintenance-restart marker — boot may be audible",
            error=str(e),
            path=str(_MAINTENANCE_RESTART_MARKER),
        )


# ── Time-string parsing ──────────────────────────────────────────────────


def _parse_hhmm(s: str) -> Optional[tuple[int, int]]:
    """Parse "HH:MM" into (hour, minute) or return None for malformed input.

    Accepts 00:00 through 23:59. Anything else (including 24:00, negative
    components, missing colon) returns None — caller falls through to the
    default time.
    """
    if not isinstance(s, str):
        return None
    parts = s.strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


# ── RSS reading ──────────────────────────────────────────────────────────


def _read_rss_mb() -> Optional[int]:
    """Read this process's resident set size in MB from /proc/self/status.

    Returns None on any read error so the caller can skip the ceiling
    check rather than mistakenly trigger a restart.
    """
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except OSError:
        pass
    return None


# ── Service ──────────────────────────────────────────────────────────────


class MaintenanceRestartService:
    """Background thread that triggers a daily restart at a configured time.

    Lifecycle: ``start()`` spawns the daemon thread; ``stop()`` signals
    it to exit cleanly on the next iteration. Idempotent.
    """

    # Defaults — overridden by settings when present in config.json.
    DEFAULT_TIME = "03:00"
    DEFAULT_RSS_CEILING_MB = 320
    POLL_INTERVAL_SECONDS = 60

    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._shutdown = threading.Event()
        # Once-per-day guard: stamp the date we triggered on so a stuck
        # clock reading the same minute twice can't double-fire.
        self._last_triggered_date: Optional[datetime.date] = None

    def start(self) -> None:
        """Spawn the polling thread. Safe to call multiple times."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._loop,
            daemon=True,
            name="maintenance-restart",
        )
        self._thread.start()

    def stop(self) -> None:
        """Signal the polling thread to exit. Doesn't wait."""
        self._shutdown.set()

    def _loop(self) -> None:
        logger.info(
            "Maintenance-restart scheduler started",
            poll_interval_s=self.POLL_INTERVAL_SECONDS,
        )
        while not self._shutdown.is_set():
            try:
                self._check_once()
            except Exception as e:
                # Never let the scheduler die from a transient error —
                # silent loss of restart safety would be a worse
                # regression than the leak itself.
                logger.error(
                    "Maintenance scheduler iteration failed",
                    error=str(e),
                )
            self._shutdown.wait(timeout=self.POLL_INTERVAL_SECONDS)

    def _check_once(self) -> None:
        """One iteration of the scheduler.

        Reads settings fresh each call so a mobile-pushed change takes
        effect on the next minute without requiring a restart of the
        subsystem.
        """
        enabled = Config.get_bool("maintenance_restart_enabled", True)
        if enabled is False:
            return

        # 1) RSS ceiling — emergency stop.
        ceiling_mb = Config.get_int(
            "maintenance_restart_rss_ceiling_mb", self.DEFAULT_RSS_CEILING_MB,
        )
        rss_mb = _read_rss_mb()
        if rss_mb is not None and ceiling_mb > 0 and rss_mb >= ceiling_mb:
            logger.warning(
                "Maintenance restart triggered by RSS ceiling — exit imminent",
                rss_mb=rss_mb,
                ceiling_mb=ceiling_mb,
            )
            self._exit_for_restart(reason="rss_ceiling")
            return  # unreachable but defensive

        # 2) Scheduled-time restart.
        time_str = Config.get_str(
            "maintenance_restart_at_time", self.DEFAULT_TIME,
        )
        parsed = _parse_hhmm(time_str)
        if parsed is None:
            # Already at default? Skip the warning to avoid noise.
            if time_str != self.DEFAULT_TIME:
                logger.warning(
                    "Invalid maintenance_restart_at_time — using default",
                    configured=time_str, default=self.DEFAULT_TIME,
                )
            parsed = _parse_hhmm(self.DEFAULT_TIME)
            assert parsed is not None  # default is valid by construction

        target_hour, target_minute = parsed
        now = datetime.datetime.now()
        today = now.date()

        if self._last_triggered_date == today:
            # Already fired today — wait for tomorrow's window.
            return

        if now.hour == target_hour and now.minute == target_minute:
            logger.info(
                "Maintenance restart triggered by schedule — exit imminent",
                configured_time=time_str,
                local_now=now.strftime("%H:%M"),
            )
            self._last_triggered_date = today
            self._exit_for_restart(reason="scheduled")

    def _exit_for_restart(self, *, reason: str) -> None:
        """Exit cleanly so systemd's Restart=always brings us back up.

        Drops the silent-restart marker BEFORE the sleep so that even if
        the 2 s flush window gets cut short by an aggressive systemd
        TimeoutStopSec, the next boot still recognises this exit as a
        maintenance trigger and stays quiet on warmup. Same belt-and-
        braces as the package-install marker.

        The short sleep gives the just-logged "exit imminent" line time
        to flush through the JarvisLogger queue + atexit hooks before
        the process dies. os._exit (vs sys.exit) bypasses any
        registered handler that might otherwise convert a graceful exit
        into a 30 s TimeoutStopSec wait.
        """
        _write_maintenance_restart_marker(reason)
        time.sleep(2.0)
        # noinspection PyProtectedMember
        os._exit(0)


# ── Module-level singleton accessor ──────────────────────────────────────


_singleton: Optional[MaintenanceRestartService] = None
_singleton_lock = threading.Lock()


def get_maintenance_restart_service() -> MaintenanceRestartService:
    """Process-wide accessor. Created lazily."""
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = MaintenanceRestartService()
        return _singleton
