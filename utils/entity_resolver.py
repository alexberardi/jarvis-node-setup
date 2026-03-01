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

# Voice command action verbs → preferred entity domain.
# When the voice command contains these words and the LLM picks the wrong
# domain, we also search the correct domain for better matches.
ACTION_VERB_DOMAINS: dict[str, str] = {
    "lock": "lock",
    "unlock": "lock",
    "open": "cover",
    "close": "cover",
    "play": "media_player",
    "pause": "media_player",
    "volume": "media_player",
    "vacuum": "vacuum",
    "clean": "vacuum",
}

# Reverse mapping: domain → action verbs that confirm the domain is correct.
# Used to skip cross-domain search when the LLM's domain already matches.
DOMAIN_ACTION_VERBS: dict[str, frozenset[str]] = {
    "light": frozenset({"light", "lights", "bright", "dim"}),
    "switch": frozenset({"switch"}),
    "lock": frozenset({"lock", "unlock"}),
    "cover": frozenset({"open", "close", "garage", "blinds"}),
    "media_player": frozenset({"play", "pause", "volume", "music"}),
    "vacuum": frozenset({"vacuum", "clean"}),
    "fan": frozenset({"fan"}),
    "climate": frozenset({"thermostat", "temperature", "heat", "cool", "ac"}),
}

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

    If entity_id matches an existing entity exactly AND its area matches the
    voice command context, returns it as-is. Otherwise, fuzzy-matches against
    entities using:
    1. Entity ID string similarity
    2. Voice command word overlap with friendly names
    3. Area/room matching from agent context

    Args:
        entity_id: Entity ID from the LLM (may not exist in HA)
        voice_command: Original voice command for name overlap scoring

    Returns:
        Resolved entity_id (original if exact match or no good fuzzy match)
    """
    registry = _get_entity_registry()
    if not registry:
        return entity_id

    # Exact match — but validate area if voice command mentions a room
    if any(e.entity_id == entity_id for e in registry):
        corrected = _validate_area_match(entity_id, voice_command, registry)
        if corrected:
            return corrected
        return entity_id

    # Extract domain for filtering
    if "." not in entity_id:
        return entity_id
    domain = entity_id.split(".", 1)[0]
    query_name = entity_id.split(".", 1)[1]

    # Check if voice command implies a different domain than the LLM picked.
    # e.g., "lock the front door" + switch.front_door → should search lock.*
    # BUT: skip cross-domain if the LLM's domain already matches action verbs
    # in the voice command. This prevents "turn off lights AND unlock door"
    # from cross-domain resolving the light entity to a lock entity.
    correct_domain = _infer_domain_from_voice(voice_command)
    search_domains = [domain]
    if correct_domain and correct_domain != domain:
        # Only cross-domain if the LLM's domain doesn't match the voice command
        if not _domain_matches_voice(domain, voice_command):
            search_domains.insert(0, correct_domain)

    # Build entity→area map from agent context
    entity_area_map = _get_entity_area_map()

    best_score = 0.0
    best_entity: Optional[EntityInfo] = None
    best_method = "id_similarity"

    for search_domain in search_domains:
        candidates = [e for e in registry if e.entity_id.startswith(f"{search_domain}.")]
        if not candidates:
            continue

        # Cross-domain matches get a bonus since the LLM picked the wrong domain
        # but the voice command clearly indicates the correct one
        domain_bonus = 0.1 if search_domain == correct_domain and search_domain != domain else 0.0

        for candidate in candidates:
            candidate_name = candidate.entity_id.split(".", 1)[1]

            id_score = _compute_id_score(query_name, candidate_name)
            name_score = _compute_name_score(voice_command, candidate.friendly_name)
            area_score = _compute_area_score(
                voice_command, entity_id, candidate.entity_id, entity_area_map
            )

            # Pick the best signal for this candidate
            score = id_score
            method = "id_similarity"
            if name_score > score:
                score = name_score
                method = "name_overlap"
            if area_score > score:
                score = area_score
                method = "area_match"

            # Apply cross-domain bonus
            score = min(score + domain_bonus, 1.0)
            if domain_bonus > 0:
                method = f"cross_domain_{method}"

            if score > best_score:
                best_score = score
                best_entity = candidate
                best_method = method

    if best_entity and best_score >= MATCH_THRESHOLD:
        logger.info(
            "Fuzzy-resolved entity",
            original=entity_id,
            resolved=best_entity.entity_id,
            score=round(best_score, 3),
            method=best_method,
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


def _validate_area_match(
    entity_id: str,
    voice_command: str,
    registry: List[EntityInfo],
) -> Optional[str]:
    """Check if an exact-match entity's area matches the voice command.

    Catches cases where the LLM picks an entity by name similarity
    (e.g., light.hue_play_2 for "play room") but the entity is in a
    different area. If so, searches for a better entity in the correct area.

    Args:
        entity_id: The exact-match entity ID
        voice_command: Original voice command
        registry: Full entity registry

    Returns:
        Better entity_id if area mismatch found, or None if original is fine
    """
    if not voice_command:
        return None

    entity_area_map = _get_entity_area_map()
    if not entity_area_map:
        return None

    entity_area = entity_area_map.get(entity_id, "")

    # Extract room names from voice command by checking against known areas.
    # Normalize both sides: "playroom" should match "Play room", etc.
    all_areas = set(entity_area_map.values())
    cmd_lower = voice_command.lower()
    cmd_nospaces = cmd_lower.replace(" ", "")

    mentioned_areas: list[str] = []
    for area in all_areas:
        area_lower = area.lower()
        area_nospaces = area_lower.replace(" ", "")
        if area_lower in cmd_lower or area_nospaces in cmd_nospaces:
            mentioned_areas.append(area)

    if not mentioned_areas:
        return None

    # If the entity's area matches one of the mentioned areas, it's fine.
    # Also check normalized form (e.g., entity area "Play room" vs mentioned "Play room")
    if entity_area in mentioned_areas:
        return None
    entity_area_nospaces = entity_area.lower().replace(" ", "") if entity_area else ""
    for ma in mentioned_areas:
        if ma.lower().replace(" ", "") == entity_area_nospaces:
            return None

    # Entity's area doesn't match what the user asked for.
    # Determine which area this entity should be in.
    domain = entity_id.split(".", 1)[0] if "." in entity_id else ""
    if not domain:
        return None

    # When multiple areas are mentioned, disambiguate by checking which area
    # the entity ID name is most similar to. This prevents "hue_color_lamp_3"
    # (no area hint) from being blindly corrected to the wrong room.
    target_area: Optional[str] = None
    if len(mentioned_areas) == 1:
        target_area = mentioned_areas[0]
    else:
        # Multiple areas mentioned — use entity ID name similarity to pick
        entity_name = entity_id.split(".", 1)[-1].replace("_", " ").lower()
        best_sim = 0.0
        for area in mentioned_areas:
            sim = SequenceMatcher(None, entity_name, area.lower()).ratio()
            if sim > best_sim:
                best_sim = sim
                target_area = area
        # Only proceed if there's a clear match (> 0.4 similarity)
        if best_sim <= 0.4:
            logger.debug(
                "Area-correction skipped: ambiguous multi-room command",
                entity_id=entity_id,
                mentioned_areas=mentioned_areas,
                best_sim=round(best_sim, 3),
            )
            return None

    if not target_area:
        return None

    # Find a same-domain entity in the target area
    for eid, area in entity_area_map.items():
        if area == target_area and eid.startswith(f"{domain}.") and eid != entity_id:
            logger.info(
                "Area-corrected entity",
                original=entity_id,
                original_area=entity_area or "(none)",
                resolved=eid,
                resolved_area=target_area,
            )
            return eid

    return None


def _domain_matches_voice(domain: str, voice_command: str) -> bool:
    """Check if the LLM's chosen domain is consistent with the voice command.

    Returns True if the voice command contains words that confirm the domain
    is correct (e.g., "lights" confirms "light" domain). Used to prevent
    cross-domain search when the original domain already makes sense.

    Args:
        domain: The LLM's chosen entity domain
        voice_command: Original voice command text

    Returns:
        True if the domain is consistent with the voice command
    """
    if not voice_command:
        return False

    confirming_verbs = DOMAIN_ACTION_VERBS.get(domain)
    if not confirming_verbs:
        return False

    cmd_words = set(voice_command.lower().split())
    return bool(confirming_verbs & cmd_words)


def _infer_domain_from_voice(voice_command: str) -> Optional[str]:
    """Infer the correct entity domain from action verbs in the voice command.

    Maps action verbs like "lock", "unlock", "open", "close" to their
    corresponding HA entity domains so the resolver can search the right
    domain even if the LLM picked the wrong one.

    Args:
        voice_command: Original voice command text

    Returns:
        Inferred domain string (e.g., "lock") or None if no match
    """
    if not voice_command:
        return None

    cmd_lower = voice_command.lower()
    cmd_words = set(cmd_lower.split())

    for verb, domain in ACTION_VERB_DOMAINS.items():
        if verb in cmd_words:
            return domain

    return None


def _get_entity_area_map() -> dict[str, str]:
    """Get entity_id → area name mapping from the HA agent context.

    Returns:
        Dict mapping entity_id to area name, or empty dict.
    """
    try:
        from services.agent_scheduler_service import get_agent_scheduler_service
        context = get_agent_scheduler_service().get_aggregated_context()
        ha_data = context.get("home_assistant", {})
        device_controls = ha_data.get("device_controls", {})

        area_map: dict[str, str] = {}
        for domain_devices in device_controls.values():
            for dev in domain_devices:
                eid = dev.get("entity_id", "")
                area = dev.get("area", "")
                if eid and area:
                    area_map[eid] = area
        return area_map
    except Exception:
        return {}


def _compute_area_score(
    voice_command: str,
    query_entity_id: str,
    candidate_entity_id: str,
    entity_area_map: dict[str, str],
) -> float:
    """Score a candidate based on area/room matching.

    If the voice command or the hallucinated entity ID contains a room name
    that matches the candidate's area, return a high score.

    Args:
        voice_command: Original voice command
        query_entity_id: The (possibly hallucinated) entity ID from the LLM
        candidate_entity_id: Real entity ID being evaluated
        entity_area_map: entity_id → area name mapping

    Returns:
        Score 0.0-1.0
    """
    area = entity_area_map.get(candidate_entity_id, "")
    if not area:
        return 0.0

    area_lower = area.lower()
    area_words = set(area_lower.split())

    # Check the hallucinated entity ID for area similarity FIRST.
    # This is the most specific signal — "light.playroom" should strongly
    # prefer "Play Room" over "Living Room" even if both appear in the
    # voice command.
    query_name = query_entity_id.split(".", 1)[-1]
    query_name_normalized = query_name.replace("_", " ").lower()
    entity_id_area_sim = SequenceMatcher(None, query_name_normalized, area_lower).ratio()
    if entity_id_area_sim > 0.7:
        return 0.95  # Very strong: entity ID name matches candidate's area

    # Word-level check for entity ID → area (e.g., "living_room" → "Living Room")
    query_words = set(query_name_normalized.split())
    if area_words and area_words.issubset(query_words):
        return 0.9

    # Check voice command for area words (weaker signal when multi-room)
    if voice_command:
        cmd_lower = voice_command.lower()
        area_nospaces = area_lower.replace(" ", "")
        cmd_nospaces = cmd_lower.replace(" ", "")

        # Exact area match (with or without spaces): "play room" or "playroom"
        if area_lower in cmd_lower or area_nospaces in cmd_nospaces:
            return 0.85

        cmd_words = {w for w in cmd_lower.split() if w not in STOP_WORDS}
        if area_words and area_words.issubset(cmd_words):
            return 0.8

    return 0.0


def clear_entity_registry_cache() -> None:
    """Reset the entity registry cache. Useful for tests."""
    global _entity_registry_cache
    _entity_registry_cache = None
