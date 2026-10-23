"""
Flatten a custom agent's get_context_data() into memory facts.

jarvisd only reads the ``home_assistant`` and ``device_agent`` entries of the
agent payload; every other agent's context reaches the LLM as memory facts
sent to POST /api/v0/memories/inject, which jarvisd retrieves per turn by
relevance. Each leaf of the context becomes one short line
(``"<agent>: <path>: <value>"``) with a stable key per agent + path, so a
later refresh replaces the fact instead of piling up a new one.
"""

import hashlib
from typing import Any, Dict, List, Tuple

from clients.rest_client import MemoryItem

# Agents whose context jarvisd already reads from the agent payload.
PAYLOAD_AGENTS = frozenset({"home_assistant", "device_agent"})

# Bookkeeping keys agents put in their context; not facts about the world.
SKIPPED_KEYS = frozenset({"last_error", "error_time"})

MEMORY_CATEGORY = "agent_context"

# Caps. The server takes at most 100 items per request, so one agent's facts
# always fit in one call.
MAX_FACTS_PER_AGENT = 50
MAX_LIST_ITEMS = 10
MAX_VALUE_LEN = 300
MAX_DEPTH = 6

# Server limits on /memories/inject fields.
MAX_CONTENT_LEN = 2000
MAX_KEY_LEN = 500
MAX_SOURCE_LEN = 100
MAX_TTL_HOURS = 720.0
MIN_TTL_HOURS = 1.0

Fact = Tuple[str, str]  # (key path, fact text)


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _is_empty(value: Any) -> bool:
    return value is None or (isinstance(value, (str, list, tuple, dict)) and len(value) == 0)


def _is_scalar(value: Any) -> bool:
    return not isinstance(value, (dict, list, tuple))


def flatten_context(agent_name: str, data: Dict[str, Any]) -> List[Fact]:
    """Flatten an agent's context dict into ``(path, text)`` facts.

    Dict keys are walked in sorted order so paths and the cap are stable run
    to run. A list of scalars becomes one comma-joined fact; a list of dicts
    or lists is walked item by item (1-based in the text, ``[i]`` in the key).
    Empty values and error bookkeeping keys are skipped.
    """
    if not isinstance(data, dict):
        return []

    facts: List[Fact] = []

    def emit(key_path: str, text_path: List[str], value: Any) -> None:
        value_text = _truncate(str(value), MAX_VALUE_LEN)
        facts.append((key_path, ": ".join([agent_name, *text_path, value_text])))

    def walk(value: Any, key_path: str, text_path: List[str], depth: int) -> None:
        if len(facts) >= MAX_FACTS_PER_AGENT or _is_empty(value):
            return
        if _is_scalar(value):
            emit(key_path, text_path, value)
            return
        if depth >= MAX_DEPTH:
            return
        if isinstance(value, dict):
            for k in sorted(value, key=str):
                if depth == 0 and k in SKIPPED_KEYS:
                    continue
                child_key = f"{key_path}.{k}" if key_path else str(k)
                walk(value[k], child_key, [*text_path, str(k)], depth + 1)
            return
        items = [v for v in value if not _is_empty(v)][:MAX_LIST_ITEMS]
        if all(_is_scalar(v) for v in items):
            emit(key_path, text_path, ", ".join(str(v) for v in items))
            return
        for i, item in enumerate(items):
            walk(item, f"{key_path}[{i}]", [*text_path, str(i + 1)], depth + 1)

    walk(data, "", [], 0)
    return facts[:MAX_FACTS_PER_AGENT]


def ttl_hours_for_interval(interval_seconds: float) -> float:
    """TTL for an agent's facts: twice its refresh interval, so a late refresh
    leaves no gap, within the server's bounds."""
    hours = 2 * max(0.0, float(interval_seconds)) / 3600
    return min(MAX_TTL_HOURS, max(MIN_TTL_HOURS, hours))


def _memory_key(agent_name: str, path: str) -> str:
    key = f"agent:{agent_name}:{path}"
    if len(key) <= MAX_KEY_LEN:
        return key
    digest = hashlib.sha256(key.encode()).hexdigest()[:16]
    return f"{key[: MAX_KEY_LEN - len(digest) - 1]}#{digest}"


def build_memory_items(
    agent_name: str, data: Dict[str, Any], interval_seconds: float
) -> List[MemoryItem]:
    """Memory items for /memories/inject from an agent's context (household-wide)."""
    ttl = ttl_hours_for_interval(interval_seconds)
    source = f"agent:{agent_name}"[:MAX_SOURCE_LEN]
    return [
        {
            "content": _truncate(text, MAX_CONTENT_LEN),
            "key": _memory_key(agent_name, path),
            "category": MEMORY_CATEGORY,
            "source": source,
            "ttl_hours": ttl,
        }
        for path, text in flatten_context(agent_name, data)
    ]
