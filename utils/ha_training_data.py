"""
Home Assistant training data utilities.

Fetches real HA entity data and uses a template system to dynamically generate
adapter training examples. This ensures the LoRA adapter trains on entity IDs
that actually exist in the user's Home Assistant installation.

Template tokens:
    {{NAME}}       - Entity friendly name (lowercased)
    {{ROOM}}       - Area/room name (from entity_id or area data)
    {{SCENE_TYPE}} - Scene variant (e.g., "read", "bright", "dimmed")
"""

import asyncio
import concurrent.futures
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from jarvis_log_client import JarvisLogger

from core.ijarvis_command import CommandExample

logger = JarvisLogger(service="jarvis-node")

# Module-level cache: first command to call fetches, second gets cached.
# Not thread-safe, but training is a single-threaded batch operation.
_ha_data_cache: Optional[Dict[str, Any]] = None
_ha_data_fetched: bool = False

# Functional scene types worth training on (skip decorative/color themes)
FUNCTIONAL_SCENE_TYPES = {
    "bright", "dimmed", "read", "nightlight", "relax", "concentrate", "energize",
}

# Per-domain caps to keep example count reasonable
DOMAIN_CAPS = {
    "light": 8,
    "switch": 6,
    "scene": 15,
    "cover": 4,
    "lock": 4,
    "climate": 3,
    "fan": 3,
    "vacuum": 2,
}

# Patterns for entities to skip during filtering
_SKIP_PATTERNS = [
    re.compile(r"_pre_release$"),              # HACS toggles
    re.compile(r"_sensor_.*_enabled$"),         # Sensor config switches
    re.compile(r"^light\.hue_.*_lamp_\d+$"),   # Individual Hue bulbs
    re.compile(r"^light\.hue_play_\d+$"),      # Individual Hue Play bars
    re.compile(r"^switch\.tz3210_"),            # Hex/model number names
    re.compile(r"^switch\.third_reality_inc_"), # Third Reality model names
    re.compile(r"^light\.third_reality_inc_"),  # Third Reality model names
    re.compile(r"_touch_control$"),             # Touch control sub-entities
]


# ---------------------------------------------------------------------------
# Built-in templates
# ---------------------------------------------------------------------------

CONTROL_TEMPLATES: Dict[str, List[Dict[str, Any]]] = {
    "light": [
        {
            "action": "turn_on",
            "utterances": [
                "Turn on the {{NAME}} lights",
                "Turn on the {{NAME}}",
                "{{NAME}} lights on",
                "Switch on the {{NAME}} light",
            ],
        },
        {
            "action": "turn_off",
            "utterances": [
                "Turn off the {{NAME}} lights",
                "Kill the {{NAME}} lights",
                "{{NAME}} lights off",
            ],
        },
    ],
    "switch": [
        {"action": "turn_on", "utterances": ["Turn on the {{NAME}}", "{{NAME}} on"]},
        {"action": "turn_off", "utterances": ["Turn off the {{NAME}}", "{{NAME}} off"]},
    ],
    "scene": [
        {
            "action": "turn_on",
            "utterances": [
                "Activate the {{NAME}} scene",
                "Set the {{ROOM}} to {{SCENE_TYPE}}",
            ],
        },
    ],
    "cover": [
        {"action": "open_cover", "utterances": ["Open the {{NAME}}", "Open the {{NAME}} door"]},
        {"action": "close_cover", "utterances": ["Close the {{NAME}}", "Close the {{NAME}} door"]},
        {"action": "stop_cover", "utterances": ["Stop the {{NAME}}"]},
    ],
    "lock": [
        {"action": "lock", "utterances": ["Lock the {{NAME}}"]},
        {"action": "unlock", "utterances": ["Unlock the {{NAME}}"]},
    ],
    "climate": [
        {"action": "turn_on", "utterances": ["Turn on the {{NAME}}"]},
        {"action": "turn_off", "utterances": ["Turn off the {{NAME}}"]},
        {
            "action": "set_temperature",
            "utterances": ["Set the {{NAME}} to 72"],
            "extra_params": {"value": "72"},
        },
    ],
    "vacuum": [
        {"action": "start", "utterances": ["Start the {{NAME}}", "Run the {{NAME}}"]},
        {"action": "stop", "utterances": ["Stop the {{NAME}}"]},
        {"action": "return_to_base", "utterances": ["Send {{NAME}} home"]},
    ],
    "fan": [
        {"action": "turn_on", "utterances": ["Turn on the {{NAME}}"]},
        {"action": "turn_off", "utterances": ["Turn off the {{NAME}}"]},
    ],
}

STATUS_TEMPLATES: Dict[str, List[Dict[str, Any]]] = {
    "light": [
        {"utterances": ["Is the {{NAME}} light on?", "Are the {{NAME}} lights on?", "Check the {{NAME}} lights"]},
    ],
    "switch": [
        {"utterances": ["Is the {{NAME}} on?", "Check the {{NAME}}"]},
    ],
    "cover": [
        {"utterances": ["Is the {{NAME}} open?", "Check the {{NAME}}"]},
    ],
    "lock": [
        {"utterances": ["Is the {{NAME}} locked?", "Check if the {{NAME}} is locked"]},
    ],
    "climate": [
        {"utterances": ["What's the {{NAME}} set to?", "Check the {{NAME}}"]},
    ],
    "vacuum": [
        {"utterances": ["What's the {{NAME}} doing?", "Check the {{NAME}}"]},
    ],
    "fan": [
        {"utterances": ["Is the {{NAME}} on?", "Check the {{NAME}}"]},
    ],
}


# ---------------------------------------------------------------------------
# HA data fetch + cache
# ---------------------------------------------------------------------------

def get_ha_training_data() -> Optional[Dict[str, Any]]:
    """Fetch HA device data, with module-level caching.

    Returns device_controls and light_controls dicts from HomeAssistantAgent,
    or None if HA is unreachable or secrets are missing.

    Returns:
        Dict with 'device_controls' and 'light_controls' keys, or None
    """
    global _ha_data_cache, _ha_data_fetched

    if _ha_data_fetched:
        return _ha_data_cache

    _ha_data_fetched = True

    try:
        from agents.home_assistant_agent import HomeAssistantAgent

        agent = HomeAssistantAgent()

        # Run the async fetch synchronously.
        # Prefer asyncio.run() directly (matches how test_ha_agent.py works).
        # Fall back to thread pool only if already inside a running event loop.
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(asyncio.run, agent.run()).result(timeout=60)
        else:
            asyncio.run(agent.run())

        context = agent.get_context_data()

        if context.get("last_error"):
            logger.warning(
                "HA training data fetch had error, falling back to static",
                error=context["last_error"],
            )
            _ha_data_cache = None
            return None

        _ha_data_cache = {
            "device_controls": context.get("device_controls", {}),
            "light_controls": context.get("light_controls", {}),
        }
        logger.info(
            "HA training data fetched",
            domains=list(_ha_data_cache["device_controls"].keys()),
        )
        return _ha_data_cache

    except Exception as e:
        logger.warning("Failed to fetch HA training data", error=str(e))
        _ha_data_cache = None
        return None


def clear_ha_training_cache() -> None:
    """Clear the module-level cache. Useful for testing."""
    global _ha_data_cache, _ha_data_fetched
    _ha_data_cache = None
    _ha_data_fetched = False


# ---------------------------------------------------------------------------
# Template loading
# ---------------------------------------------------------------------------

def load_templates(
    template_type: str = "control",
) -> Dict[str, List[Dict[str, Any]]]:
    """Load built-in templates and merge optional JSON overrides.

    Args:
        template_type: "control" or "status"

    Returns:
        Merged template dict: domain -> list of template entries
    """
    if template_type == "control":
        # Shallow-copy outer lists so appending JSON overrides doesn't mutate constants
        merged = {k: list(v) for k, v in CONTROL_TEMPLATES.items()}
    else:
        merged = {k: list(v) for k, v in STATUS_TEMPLATES.items()}

    # Try loading optional JSON override
    json_path = Path(__file__).parent.parent / "additional_home_assistant_device_mappings.json"
    if json_path.exists():
        try:
            with open(json_path) as f:
                overrides = json.load(f)

            key = "control_templates" if template_type == "control" else "status_templates"
            json_templates = overrides.get(key, {})

            # Append JSON templates to built-in (don't replace)
            for domain, entries in json_templates.items():
                if domain not in merged:
                    merged[domain] = []
                merged[domain].extend(entries)

            logger.info("Loaded template overrides", template_type=template_type, domains=list(json_templates.keys()))
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load template overrides", error=str(e))

    return merged


# ---------------------------------------------------------------------------
# Entity filtering
# ---------------------------------------------------------------------------

def filter_entities(entities: List[Dict[str, Any]], domain: str) -> List[Dict[str, Any]]:
    """Filter out diagnostic, config, and noisy entities.

    Args:
        entities: List of entity dicts from device_controls
        domain: The domain being filtered (e.g., "light", "switch")

    Returns:
        Filtered list of entities suitable for training
    """
    filtered = []
    for entity in entities:
        entity_id = entity.get("entity_id", "")

        if _should_skip_entity(entity_id):
            continue

        filtered.append(entity)

    # Apply per-domain cap
    cap = DOMAIN_CAPS.get(domain, 10)
    return filtered[:cap]


def _should_skip_entity(entity_id: str) -> bool:
    """Check if an entity should be skipped based on filtering rules."""
    return any(pattern.search(entity_id) for pattern in _SKIP_PATTERNS)


def filter_scenes(
    scene_entities: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Filter scenes to only functional types (bright, dimmed, read, etc.).

    Args:
        scene_entities: List of scene entity dicts from device_controls

    Returns:
        Filtered list of functional scenes
    """
    filtered = []
    for entity in scene_entities:
        entity_id = entity.get("entity_id", "")
        scene_type = _parse_scene_type(entity_id)

        if scene_type and scene_type in FUNCTIONAL_SCENE_TYPES:
            filtered.append(entity)

    cap = DOMAIN_CAPS.get("scene", 15)
    return filtered[:cap]


# ---------------------------------------------------------------------------
# Template hydration
# ---------------------------------------------------------------------------

def _parse_scene_type(entity_id: str) -> Optional[str]:
    """Extract the scene type from a scene entity_id.

    Example: 'scene.office_desk_read' -> 'read'
             'scene.basement_bright' -> 'bright'
             'scene.my_office_dimmed' -> 'dimmed'
    """
    if not entity_id.startswith("scene."):
        return None

    # The suffix after the last known room/device segment
    name = entity_id.split(".", 1)[1]
    parts = name.rsplit("_", 1)
    if len(parts) == 2:
        return parts[1]
    return None


def _parse_scene_room(entity_id: str) -> Optional[str]:
    """Extract the room/device from a scene entity_id.

    Example: 'scene.office_desk_read' -> 'office desk'
             'scene.basement_bright' -> 'basement'
             'scene.my_office_dimmed' -> 'my office'
    """
    if not entity_id.startswith("scene."):
        return None

    name = entity_id.split(".", 1)[1]
    parts = name.rsplit("_", 1)
    if len(parts) == 2:
        return parts[0].replace("_", " ")
    return None


def hydrate_template(
    utterance: str,
    entity: Dict[str, Any],
) -> Optional[str]:
    """Replace template tokens with entity data.

    Args:
        utterance: Template string with {{NAME}}, {{ROOM}}, {{SCENE_TYPE}} tokens
        entity: Entity dict with entity_id, name, state, etc.

    Returns:
        Hydrated string, or None if a required token can't be resolved
    """
    entity_id = entity.get("entity_id", "")
    friendly_name = entity.get("name", "")

    result = utterance

    if "{{NAME}}" in result:
        if not friendly_name:
            return None
        result = result.replace("{{NAME}}", friendly_name.lower())

    if "{{ROOM}}" in result:
        room = _parse_scene_room(entity_id)
        if not room:
            return None
        result = result.replace("{{ROOM}}", room)

    if "{{SCENE_TYPE}}" in result:
        scene_type = _parse_scene_type(entity_id)
        if not scene_type:
            return None
        result = result.replace("{{SCENE_TYPE}}", scene_type)

    return result


# ---------------------------------------------------------------------------
# Example generation
# ---------------------------------------------------------------------------

def generate_control_examples(
    device_controls: Dict[str, List[Dict[str, Any]]],
    light_controls: Dict[str, Dict[str, Any]],
) -> List[CommandExample]:
    """Generate control command training examples from real HA entities.

    Args:
        device_controls: Domain-grouped entity dicts from HomeAssistantAgent
        light_controls: Room-grouped light entity dicts

    Returns:
        List of CommandExample for adapter training
    """
    templates = load_templates("control")
    examples: List[CommandExample] = []
    seen_utterances: set = set()

    # Process light_controls first (room groups are the primary light entities)
    light_entities = _light_controls_to_entity_list(light_controls)
    _generate_for_domain(
        "light", light_entities, templates, examples, seen_utterances, is_control=True,
    )

    # Process other domains from device_controls
    for domain, entities in device_controls.items():
        if domain == "light":
            # Already handled via light_controls
            continue

        if domain == "scene":
            filtered = filter_scenes(entities)
        else:
            filtered = filter_entities(entities, domain)

        if not filtered:
            continue

        _generate_for_domain(
            domain, filtered, templates, examples, seen_utterances, is_control=True,
        )

    # Mark first example as primary
    if examples:
        examples[0] = CommandExample(
            voice_command=examples[0].voice_command,
            expected_parameters=examples[0].expected_parameters,
            is_primary=True,
        )

    return examples


def generate_status_examples(
    device_controls: Dict[str, List[Dict[str, Any]]],
    light_controls: Dict[str, Dict[str, Any]],
) -> List[CommandExample]:
    """Generate status query training examples from real HA entities.

    Args:
        device_controls: Domain-grouped entity dicts from HomeAssistantAgent
        light_controls: Room-grouped light entity dicts

    Returns:
        List of CommandExample for adapter training
    """
    templates = load_templates("status")
    examples: List[CommandExample] = []
    seen_utterances: set = set()

    # Process light_controls first
    light_entities = _light_controls_to_entity_list(light_controls)
    _generate_for_domain(
        "light", light_entities, templates, examples, seen_utterances, is_control=False,
    )

    # Process other domains
    for domain, entities in device_controls.items():
        if domain == "light":
            continue

        if domain == "scene":
            # Scenes don't have "status" queries
            continue

        filtered = filter_entities(entities, domain)
        if not filtered:
            continue

        _generate_for_domain(
            domain, filtered, templates, examples, seen_utterances, is_control=False,
        )

    # Mark first example as primary
    if examples:
        examples[0] = CommandExample(
            voice_command=examples[0].voice_command,
            expected_parameters=examples[0].expected_parameters,
            is_primary=True,
        )

    return examples


def _light_controls_to_entity_list(
    light_controls: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert light_controls dict to a list matching device_controls format.

    light_controls: {"Basement": {"entity_id": "light.basement", "state": "off", ...}}
    Returns: [{"entity_id": "light.basement", "name": "Basement", "state": "off"}, ...]
    """
    entities = []
    for friendly_name, info in light_controls.items():
        entities.append({
            "entity_id": info["entity_id"],
            "name": friendly_name,
            "state": info.get("state"),
        })
    return entities


def _generate_for_domain(
    domain: str,
    entities: List[Dict[str, Any]],
    templates: Dict[str, List[Dict[str, Any]]],
    examples: List[CommandExample],
    seen_utterances: set,
    is_control: bool,
) -> None:
    """Generate examples for a single domain, appending to examples list.

    Args:
        domain: Entity domain (e.g., "light", "cover")
        entities: Filtered entities for this domain
        templates: Loaded templates dict
        examples: List to append generated CommandExample to
        seen_utterances: Set of already-seen utterances (for dedup)
        is_control: True for control commands, False for status queries
    """
    domain_templates = templates.get(domain, [])
    if not domain_templates:
        return

    for entity in entities:
        entity_id = entity.get("entity_id", "")

        for template in domain_templates:
            utterances = template.get("utterances", [])
            action = template.get("action")
            extra_params = template.get("extra_params", {})

            for utterance_template in utterances:
                hydrated = hydrate_template(utterance_template, entity)
                if hydrated is None:
                    continue

                # Dedup
                if hydrated.lower() in seen_utterances:
                    continue
                seen_utterances.add(hydrated.lower())

                # Build params
                if is_control:
                    params: Dict[str, Any] = {"entity_id": entity_id}
                    if action:
                        params["action"] = action
                    params.update(extra_params)
                else:
                    params = {"entity_id": entity_id}

                examples.append(CommandExample(
                    voice_command=hydrated,
                    expected_parameters=params,
                ))
