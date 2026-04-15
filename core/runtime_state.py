"""Process-wide runtime state — currently just "am I busy?" for updates.

`is_busy()` exists so the Command Center can defer triggering an update
while a voice interaction is in flight. Listeners should wrap active
work in `mark_active()` calls; after `_IDLE_AFTER_SECONDS` of no calls
the node is considered idle again.

This is intentionally simple (a single timestamp, not a stack) — it's a
heuristic to avoid interrupting the user, not a correctness guarantee.
"""

from __future__ import annotations

import threading
import time

_IDLE_AFTER_SECONDS = 60.0

_lock = threading.Lock()
_last_active_monotonic: float = 0.0


def mark_active() -> None:
    """Call at the start (and for long-running work, mid-way) of a voice
    interaction or any operation that shouldn't be interrupted by an update."""
    global _last_active_monotonic
    with _lock:
        _last_active_monotonic = time.monotonic()


def is_busy() -> bool:
    with _lock:
        if _last_active_monotonic == 0.0:
            return False
        return (time.monotonic() - _last_active_monotonic) < _IDLE_AFTER_SECONDS
