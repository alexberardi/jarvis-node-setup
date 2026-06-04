"""Wake-threshold auto-calibrator — per-node, per-mic, per-room.

Each legitimate (non not_for_me) wake records its OWW score at the
moment of fire. Once the deque has at least ``MIN_SAMPLES`` data points,
:func:`auto_calibrated_wake_threshold` returns a threshold set just under
the lowest-scoring real wake — meaning real wakes always fire on the
first attempt without enlarging the false-positive window for ambient
noise.

State is module-level (the calibrator is a singleton per process). The
in-memory deque is the authoritative store at runtime; persistence to
``wake_scores.json`` is the survives-restart mechanism. Disk failures
are logged and swallowed; calibration is best-effort.
"""

from __future__ import annotations

import json
import threading
from collections import deque

from jarvis_log_client import JarvisLogger

from utils.encryption_utils import get_cache_dir


logger = JarvisLogger(service="jarvis-node")
_cache_dir = get_cache_dir()


_WAKE_SCORE_HISTORY_FILE = _cache_dir / "wake_scores.json"
_WAKE_SCORE_HISTORY_MAX = 20
_WAKE_SCORE_HISTORY_MIN_SAMPLES = 5

_wake_score_history: deque[float] = deque(maxlen=_WAKE_SCORE_HISTORY_MAX)
_wake_score_history_lock = threading.Lock()
_wake_score_history_loaded = False


def load_wake_score_history() -> None:
    """Load persisted wake-score history into the in-memory deque
    (idempotent — only reads disk on the first call per process)."""
    global _wake_score_history_loaded
    if _wake_score_history_loaded:
        return
    try:
        if _WAKE_SCORE_HISTORY_FILE.exists():
            data = json.loads(_WAKE_SCORE_HISTORY_FILE.read_text())
            if isinstance(data, list):
                with _wake_score_history_lock:
                    _wake_score_history.clear()
                    for s in data[-_WAKE_SCORE_HISTORY_MAX:]:
                        if isinstance(s, (int, float)) and 0.0 <= float(s) <= 1.0:
                            _wake_score_history.append(float(s))
    except (OSError, ValueError) as e:
        logger.warning("Failed to load wake score history", error=str(e))
    finally:
        _wake_score_history_loaded = True


def record_legitimate_wake_score(score: float) -> None:
    """Track a wake-fire score that produced a non-not_for_me interaction.

    Each legitimate wake is one data point telling the calibrator "this is
    what 'hey jarvis' from the user sounds like in this room." After enough
    samples, :func:`auto_calibrated_wake_threshold` puts the threshold just
    under the lowest-scoring real wake — meaning real wakes always fire on
    the first attempt without enlarging the false-positive window for
    ambient noise. Persisted to disk so calibration survives restarts.
    """
    if not (0.0 <= score <= 1.0):
        return
    with _wake_score_history_lock:
        _wake_score_history.append(float(score))
        snapshot = list(_wake_score_history)
    try:
        _WAKE_SCORE_HISTORY_FILE.write_text(json.dumps(snapshot))
    except OSError as e:
        logger.warning("Failed to persist wake score history", error=str(e))


def auto_calibrated_wake_threshold(fallback: float) -> float:
    """Return a dynamic wake threshold based on recent legitimate-wake scores.

    Uses the 20th-percentile of recent scores discounted by 15%, clamped
    to ``[0.10, 0.50]``. The p20 anchor gives us a number 80% of real wakes
    exceed; the 15% discount provides margin for normal variability.
    Falls back to ``fallback`` until we have ``_WAKE_SCORE_HISTORY_MIN_SAMPLES``
    data points (no calibration on a fresh node).
    """
    load_wake_score_history()
    with _wake_score_history_lock:
        scores = sorted(_wake_score_history)
    if len(scores) < _WAKE_SCORE_HISTORY_MIN_SAMPLES:
        return fallback
    idx = int(len(scores) * 0.20)
    p20 = scores[idx]
    calibrated = p20 * 0.85
    return max(0.10, min(0.50, calibrated))
