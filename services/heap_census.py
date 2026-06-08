"""Leak-hunting diagnostic — SIGUSR2 dumps a Python heap census to /tmp.

Triggered by `kill -SIGUSR2 <pid>`. Counts gc-tracked objects by type and
writes a sorted dump to /tmp/jarvis_soak/heap_<label>_pid<PID>_<unix_ts>.txt
with two sections: top by count and top by total `sys.getsizeof`.

Memory cost: ~5-10 MB peak while the Counter + getsizeof loop runs; nothing
retained afterward. Cheaper than tracemalloc by an order of magnitude
(tracemalloc keeps per-allocation traces — known to OOM the jarvis-node
process on a Pi Zero 2W when swap is already full).

The signal handler delegates to a daemon thread so the wake loop / IPC
server in the main thread only sees signal.signal callback latency. The
worker thread does block the GIL while iterating gc.get_objects(), so
expect a 1-3 s wake-loop pause per dump on a Pi Zero 2W.

Reentrant signals (two SIGUSR2 fast in a row) start two threads that
write to different timestamped files; harmless.
"""

from __future__ import annotations

import gc
import os
import sys
import threading
import time
from collections import Counter

_DUMP_DIR = "/tmp/jarvis_soak"


def _do_heap_census(label: str) -> None:
    try:
        gc.collect()
        objs = gc.get_objects()
        type_counts: Counter = Counter()
        type_sizes: dict[str, int] = {}
        for o in objs:
            t = type(o).__name__
            type_counts[t] += 1
            try:
                type_sizes[t] = type_sizes.get(t, 0) + sys.getsizeof(o)
            except (TypeError, ValueError):
                pass
        del objs
        gc.collect()

        os.makedirs(_DUMP_DIR, exist_ok=True)
        ts = int(time.time())
        path = f"{_DUMP_DIR}/heap_{label}_pid{os.getpid()}_{ts}.txt"
        with open(path, "w") as f:
            f.write(f"# label={label} pid={os.getpid()} ts={ts}\n")
            f.write(f"# gc.get_count()={gc.get_count()}\n")
            total_objs = sum(type_counts.values())
            total_size = sum(type_sizes.values())
            f.write(f"# total objects: {total_objs}\n")
            f.write(f"# total getsizeof: {total_size}\n\n")
            f.write("# top 60 by count\n")
            for t, c in type_counts.most_common(60):
                f.write(f"{c:>10}  {t:30}  {type_sizes.get(t, 0):>12} bytes\n")
            f.write("\n# top 60 by total getsizeof\n")
            for t, s in sorted(
                type_sizes.items(), key=lambda x: -x[1],
            )[:60]:
                f.write(f"{s:>12}  {t:30}  {type_counts.get(t, 0):>10} objs\n")
        sys.stderr.write(f"heap census dumped: {path}\n")
        sys.stderr.flush()
    except Exception as e:
        sys.stderr.write(f"heap census failed: {e}\n")
        sys.stderr.flush()


def register_sigusr2(label: str) -> None:
    """Install a SIGUSR2 handler that runs the census on a daemon thread.

    `label` distinguishes processes that share the dump dir (currently only
    "main", but kept as a parameter so a future split or sidecar can use
    "audio"/"sidecar"/etc. without filename collisions).
    """
    import signal

    def _handler(signum, frame):  # noqa: ARG001 — signal callback shape
        threading.Thread(
            target=_do_heap_census,
            args=(label,),
            name=f"heap-census-{label}",
            daemon=True,
        ).start()

    signal.signal(signal.SIGUSR2, _handler)
    sys.stderr.write(
        f"heap census registered: SIGUSR2 -> {_DUMP_DIR}/heap_{label}_*.txt\n",
    )
    sys.stderr.flush()
