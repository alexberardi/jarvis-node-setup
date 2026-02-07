"""
Fuzzy entity resolution for Home Assistant commands.

When the LLM returns an entity_id that doesn't exist in HA,
this module fuzzy-matches it against real entities using:
1. Entity ID string similarity (SequenceMatcher on the name portion)
2. Voice command word overlap with friendly names

This catches common LLM mistakes like "light.office" when the real
entity is "light.my_office".
"""

from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import List, Optional

import httpx

from jarvis_log_client import JarvisLogger

from services.secret_service import get_secret_value

logger = JarvisLogger(service="jarvis-node")

MATCH_THRESHOLD = 0.55

# Stop words filtered from voice commands before name overlap scoring
STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "was", "were", "be", "been",
    "do", "does", "did", "has", "have", "had", "will", "would",
    "can", "could", "should", "may", "might", "shall",
    "my", "your", "his", "her", "its", "our", "their",
    "this", "that", "these", "those",
    "i", "me", "you", "he", "she", "it", "we", "they",
    "what", "which", "who", "whom", "where", "when", "how",
    "not", "no", "yes", "so", "if", "or", "and", "but",
    "to", "of", "in", "on", "at", "for", "with", "from", "by",
    "up", "out", "off", "over", "under", "into",
    "set", "get", "check", "tell", "show", "give",
    "please", "just", "also", "very", "really",
    "turn", "switch",
})


@dataclass
class EntityInfo:
    """Minimal entity information for matching."""
    entity_id: str
    friendly_name: str


# Module-level cache for entity registry
_entity_registry_cache: Optional[List[EntityInfo]] = None


def resolve_entity_id(entity_id: str, voice_command: str = "") -> str:
    """Resolve an entity_id against the real HA entity registry.

    If entity_id matches an existing entity exactly, returns it as-is.
    Otherwise, fuzzy-matches against entities in the same domain.

    Args:
        entity_id: Entity ID from the LLM (may not exist in HA)
        voice_command: Original voice command for name overlap scoring

    Returns:
        Resolved entity_id (original if exact match or no good fuzzy match)
    """
    registry = _get_entity_registry()
    if not registry:
        return entity_id

    # Exact match — return immediately
    if any(e.entity_id == entity_id for e in registry):
        return entity_id

    # Extract domain for filtering
    if "." not in entity_id:
        return entity_id
    domain = entity_id.split(".", 1)[0]
    query_name = entity_id.split(".", 1)[1]

    # Filter candidates to same domain
    candidates = [e for e in registry if e.entity_id.startswith(f"{domain}.")]
    if not candidates:
        return entity_id

    best_score = 0.0
    best_entity: Optional[EntityInfo] = None
    best_via_name = False

    for candidate in candidates:
        candidate_name = candidate.entity_id.split(".", 1)[1]

        id_score = _compute_id_score(query_name, candidate_name)
        name_score = _compute_name_score(voice_command, candidate.friendly_name)

        if name_score >= id_score:
            score = name_score
            via_name = True
        else:
            score = id_score
            via_name = False

        # Prefer name_overlap on tie (voice command is a stronger signal)
        if score > best_score or (score == best_score and via_name and not best_via_name):
            best_score = score
            best_entity = candidate
            best_via_name = via_name

    if best_entity and best_score >= MATCH_THRESHOLD:
        logger.info(
            "Fuzzy-resolved entity",
            original=entity_id,
            resolved=best_entity.entity_id,
            score=round(best_score, 3),
            method="name_overlap" if best_via_name else "id_similarity",
        )
        return best_entity.entity_id

    logger.debug(
        "No fuzzy match found above threshold",
        entity_id=entity_id,
        best_score=round(best_score, 3) if best_score > 0 else 0,
        threshold=MATCH_THRESHOLD,
    )
    return entity_id


def _get_entity_registry() -> List[EntityInfo]:
    """Fetch and cache the HA entity registry via GET /api/states.

    Returns:
        List of EntityInfo, or empty list on failure
    """
    global _entity_registry_cache

    if _entity_registry_cache is not None:
        return _entity_registry_cache

    try:
        base_url = get_secret_value("HOME_ASSISTANT_REST_URL", "integration")
        api_key = get_secret_value("HOME_ASSISTANT_API_KEY", "integration")
    except (ValueError, Exception):
        logger.debug("HA credentials not available for entity resolution")
        _entity_registry_cache = []
        return _entity_registry_cache

    if not base_url or not api_key:
        _entity_registry_cache = []
        return _entity_registry_cache

    url = f"{base_url.rstrip('/')}/api/states"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        response = httpx.get(url, headers=headers, timeout=10.0)
        response.raise_for_status()
        states = response.json()

        _entity_registry_cache = [
            EntityInfo(
                entity_id=s["entity_id"],
                friendly_name=s.get("attributes", {}).get("friendly_name", ""),
            )
            for s in states
            if "entity_id" in s
        ]

        logger.debug("Loaded entity registry", count=len(_entity_registry_cache))
        return _entity_registry_cache

    except httpx.HTTPStatusError as e:
        logger.warning("Failed to fetch HA states", status=e.response.status_code)
        _entity_registry_cache = []
        return _entity_registry_cache
    except (httpx.ConnectError, httpx.TimeoutException) as e:
        logger.debug("HA not reachable for entity resolution", error=str(e))
        _entity_registry_cache = []
        return _entity_registry_cache


def _compute_id_score(query_name: str, candidate_name: str) -> float:
    """Compute string similarity between entity ID name portions.

    Args:
        query_name: Name portion of query entity_id (e.g., "office")
        candidate_name: Name portion of candidate entity_id (e.g., "my_office")

    Returns:
        Similarity score 0.0-1.0
    """
    return SequenceMatcher(None, query_name, candidate_name).ratio()


def _compute_name_score(voice_command: str, friendly_name: str) -> float:
    """Compute word overlap between voice command and friendly name.

    Filters stop words from the voice command, then computes the fraction
    of friendly name words that appear in the command.

    Args:
        voice_command: Original voice command text
        friendly_name: Entity's friendly name from HA

    Returns:
        Overlap score 0.0-1.0 (fraction of name words found in command)
    """
    if not voice_command or not friendly_name:
        return 0.0

    command_words = {
        w.lower() for w in voice_command.split()
        if w.lower() not in STOP_WORDS
    }
    name_words = {w.lower() for w in friendly_name.split()}

    if not name_words:
        return 0.0

    overlap = command_words & name_words
    return len(overlap) / len(name_words)


def clear_entity_registry_cache() -> None:
    """Reset the entity registry cache. Useful for tests."""
    global _entity_registry_cache
    _entity_registry_cache = None
