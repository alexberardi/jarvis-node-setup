"""Memory hygiene for the Pi Zero node: glibc trim + an RSS/swap watchdog.

Context (June 2026 diagnosis): the node's recurring "memory leak" — the one
that makes the wake word take 7-8 s after a few hours — is NOT an unbounded
Python data structure. Every periodic structure (alert queue, token-refresh
results, discovery caches, the log-client queue) is bounded; memory measured
on prod *fluctuates* (it drops ~15 MB right after the ZWave agent frees its
full-state dump) yet the high-water mark drifts up ~17 MB/h. That is glibc
multi-arena fragmentation: a 30-thread process opens up to 8xCPU malloc arenas
(32 on the 4-core Pi Zero 2), each fragmenting independently under steady
transient-allocation churn (5-min agent fetches, the ZWave dump, per-tick
work), and glibc never returns the freed-but-fragmented pages to the OS.

Two levers, both applied elsewhere too:
  - Cap arenas (MALLOC_ARENA_MAX / mallopt M_ARENA_MAX) — done at process start
    in scripts/main.py and in the systemd unit. This module only *reports*.
  - malloc_trim() — return free pages to the OS during the idle gaps between
    agent bursts. The watchdog calls it every cycle.

The watchdog also makes the drift visible: it logs RSS+swap+thread count and
the growth slope every few minutes, and raises an ERROR if the slope crosses a
threshold. tracemalloc can't catch fragmentation (the bytes aren't tracked
Python objects), so an RSS slope monitor is the only thing that would have
caught this before it reached prod. When tracemalloc is already running
(JARVIS_TRACEMALLOC=1), the watchdog also logs the tracked-Python total: RSS
climbing while that total stays flat is the fingerprint of fragmentation
rather than a real object leak.
"""

from __future__ import annotations

import ctypes
import threading
import time

from jarvis_log_client import JarvisLogger

logger = JarvisLogger(service="jarvis-node")

# glibc only; on non-glibc platforms (macOS dev) this is a harmless no-op.
try:
    _libc: ctypes.CDLL | None = ctypes.CDLL("libc.so.6")
except Exception:
    _libc = None


def malloc_trim() -> int:
    """Ask glibc to return free heap pages to the OS. Returns 1 if memory was
    released, 0 if not, -1 when unavailable (non-glibc)."""
    if _libc is None:
        return -1
    try:
        return int(_libc.malloc_trim(0))
    except Exception:
        return -1


def read_mem_kb() -> tuple[int, int]:
    """(VmRSS, VmSwap) in kB for this process from /proc/self/status; (0, 0)
    if /proc is unavailable."""
    rss = swap = 0
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    rss = int(line.split()[1])
                elif line.startswith("VmSwap:"):
                    swap = int(line.split()[1])
    except OSError:
        pass
    return rss, swap


def read_thread_count() -> int:
    """OS thread count for this process (counts native threads too, not just
    Python threading.Thread objects). Falls back to Python's count off /proc."""
    try:
        import os

        return len(os.listdir("/proc/self/task"))
    except OSError:
        return threading.active_count()


class MemoryWatchdog:
    """Periodic RSS+swap+thread sampler with a growth-slope alarm and trim.

    Cheap by design: one /proc read, one malloc_trim, one log line per
    interval (default 5 min). Started from main(); disable with
    JARVIS_MEMORY_WATCHDOG=0.
    """

    def __init__(
        self,
        interval_seconds: float = 300.0,
        alarm_mb_per_hour: float = 12.0,
    ) -> None:
        self._interval = interval_seconds
        self._alarm_kb_per_hour = alarm_mb_per_hour * 1024
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # (monotonic, committed_kb) captured on the FIRST sample — i.e. after
        # one interval, so model-load/warmup growth isn't counted as a leak.
        self._baseline: tuple[float, int] | None = None

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="MemoryWatchdog", daemon=True
        )
        self._thread.start()
        logger.info(
            "Memory watchdog started",
            interval_s=self._interval,
            alarm_mb_per_hour=self._alarm_kb_per_hour / 1024,
            arenas_capped=(_libc is not None),
        )

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._sample()
            except Exception as e:
                logger.warning("Memory watchdog sample failed", error=str(e))

    def _sample(self) -> None:
        rss, swap = read_mem_kb()
        committed = rss + swap
        now = time.monotonic()
        threads = read_thread_count()

        slope_kb_per_hour: float | None = None
        if self._baseline is None:
            self._baseline = (now, committed)
        else:
            t0, c0 = self._baseline
            dt_h = (now - t0) / 3600.0
            if dt_h > 0:
                slope_kb_per_hour = (committed - c0) / dt_h

        fields: dict[str, object] = {
            "rss_mb": round(rss / 1024, 1),
            "swap_mb": round(swap / 1024, 1),
            "committed_mb": round(committed / 1024, 1),
            "threads": threads,
        }
        if slope_kb_per_hour is not None:
            fields["slope_mb_per_hour"] = round(slope_kb_per_hour / 1024, 2)

        # If tracemalloc is already running (JARVIS_TRACEMALLOC=1), surface the
        # tracked-Python total. committed >> tracked => fragmentation, not a
        # Python object leak.
        tracked_mb: float | None = None
        try:
            import tracemalloc

            if tracemalloc.is_tracing():
                tracked_mb = round(tracemalloc.get_traced_memory()[0] / (1024 * 1024), 1)
                fields["python_tracked_mb"] = tracked_mb
        except Exception:
            pass

        # Reclaim fragmented free pages during this idle moment.
        before_rss, _ = read_mem_kb()
        trimmed = malloc_trim()
        after_rss, _ = read_mem_kb()
        if trimmed == 1:
            fields["trim_reclaimed_mb"] = round((before_rss - after_rss) / 1024, 1)

        if slope_kb_per_hour is not None and slope_kb_per_hour > self._alarm_kb_per_hour:
            hint = ""
            if tracked_mb is not None:
                hint = (
                    " (fragmentation: RSS up, Python heap flat)"
                    if (committed / 1024) - tracked_mb > 20
                    else " (Python object growth)"
                )
            logger.error("Memory growth alarm" + hint, **fields)
        else:
            logger.info("Memory watchdog", **fields)
