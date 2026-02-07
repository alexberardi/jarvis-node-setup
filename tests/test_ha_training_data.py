"""
Unit tests for HA training data utility.

Tests template hydration, entity filtering, caching, fallback behavior,
and example generation from real HA device structures.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from utils.ha_training_data import (
    CONTROL_TEMPLATES,
    FUNCTIONAL_SCENE_TYPES,
    STATUS_TEMPLATES,
    _parse_scene_room,
    _parse_scene_type,
    _should_skip_entity,
    clear_ha_training_cache,
    filter_entities,
    filter_scenes,
    generate_control_examples,
    generate_status_examples,
    get_ha_training_data,
    hydrate_template,
    load_templates,
)


# ---------------------------------------------------------------------------
# Sample data fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_light_controls():
    """Light controls matching real HA data structure."""
    return {
        "Basement": {"entity_id": "light.basement", "state": "off", "type": "room_group"},
        "My office": {"entity_id": "light.my_office", "state": "on", "type": "room_group"},
        "Office Desk": {"entity_id": "light.office_desk", "state": "off", "type": "room_group"},
        "Office fan": {"entity_id": "light.office_fan", "state": "off", "type": "room_group"},
        "Upstairs": {"entity_id": "light.upstairs", "state": "off", "type": "room_group"},
        "Middle Bathroom": {"entity_id": "light.middle_bathroom", "state": "off", "type": "room_group"},
    }


@pytest.fixture
def sample_device_controls():
    """Device controls matching real HA data structure."""
    return {
        "light": [
            {"entity_id": "light.basement", "name": "Basement", "state": "off"},
            {"entity_id": "light.my_office", "name": "My office", "state": "on"},
            {"entity_id": "light.hue_white_lamp_6", "name": "Hue white lamp 6", "state": "off"},
            {"entity_id": "light.hue_play_2", "name": "Hue Play 2", "state": "off"},
            {"entity_id": "light.third_reality_inc_3rsnl02043z", "name": "Third Reality", "state": "off"},
        ],
        "switch": [
            {"entity_id": "switch.baby_berardi_timer", "name": "Baby Berardi Timer", "state": "off"},
            {"entity_id": "switch.hacs_pre_release", "name": "HACS Pre-release", "state": "off"},
            {"entity_id": "switch.office_sensor_motion_sensor_enabled", "name": "Office Sensor", "state": "on"},
            {"entity_id": "switch.tz3210_j4pdtz9v_ts0001", "name": "TZ3210", "state": "off"},
            {"entity_id": "switch.my_rest_toddler_lock", "name": "My Rest Toddler Lock", "state": "off"},
        ],
        "scene": [
            {"entity_id": "scene.office_desk_read", "name": "Office Desk Read", "state": "scening"},
            {"entity_id": "scene.office_desk_dimmed", "name": "Office Desk Dimmed", "state": "scening"},
            {"entity_id": "scene.basement_bright", "name": "Basement Bright", "state": "scening"},
            {"entity_id": "scene.upstairs_relax", "name": "Upstairs Relax", "state": "scening"},
            {"entity_id": "scene.middle_bathroom_nightlight", "name": "Middle Bathroom Nightlight", "state": "scening"},
            {"entity_id": "scene.office_fan_tropical_twilight", "name": "Office Fan Tropical Twilight", "state": "scening"},
            {"entity_id": "scene.upstairs_spring_blossom", "name": "Upstairs Spring Blossom", "state": "scening"},
            {"entity_id": "scene.office_desk_concentrate", "name": "Office Desk Concentrate", "state": "scening"},
            {"entity_id": "scene.office_fan_energize", "name": "Office Fan Energize", "state": "scening"},
            {"entity_id": "scene.my_office_dimmed", "name": "My Office Dimmed", "state": "scening"},
        ],
        "cover": [
            {"entity_id": "cover.garage_door", "name": "Garage Door", "state": "closed"},
        ],
    }


@pytest.fixture(autouse=True)
def clear_cache():
    """Clear module-level cache before each test."""
    clear_ha_training_cache()
    yield
    clear_ha_training_cache()


# ---------------------------------------------------------------------------
# Entity filtering tests
# ---------------------------------------------------------------------------

class TestEntityFiltering:
    """Test entity filtering rules."""

    def test_skips_pre_release_entities(self):
        """Filters out HACS pre_release toggles."""
        assert _should_skip_entity("switch.hacs_pre_release") is True
        assert _should_skip_entity("switch.baby_buddy_pre_release") is True

    def test_skips_sensor_enabled_entities(self):
        """Filters out sensor config switches."""
        assert _should_skip_entity("switch.office_sensor_motion_sensor_enabled") is True
        assert _should_skip_entity("switch.middle_bathroom_sensor_light_sensor_enabled") is True

    def test_skips_individual_hue_bulbs(self):
        """Filters out individual Hue white/color lamps."""
        assert _should_skip_entity("light.hue_white_lamp_6") is True
        assert _should_skip_entity("light.hue_color_lamp_2") is True

    def test_skips_hue_play_bars(self):
        """Filters out individual Hue Play bars."""
        assert _should_skip_entity("light.hue_play_1") is True
        assert _should_skip_entity("light.hue_play_2") is True

    def test_skips_hex_model_names(self):
        """Filters out hex/model number entity names."""
        assert _should_skip_entity("switch.tz3210_j4pdtz9v_ts0001") is True
        assert _should_skip_entity("light.third_reality_inc_3rsnl02043z") is True

    def test_skips_touch_control_sub_entities(self):
        """Filters out touch control sub-entities."""
        assert _should_skip_entity("switch.tz3210_j4pdtz9v_ts0001_touch_control") is True

    def test_keeps_normal_entities(self):
        """Keeps valid controllable entities."""
        assert _should_skip_entity("light.basement") is False
        assert _should_skip_entity("switch.baby_berardi_timer") is False
        assert _should_skip_entity("cover.garage_door") is False
        assert _should_skip_entity("lock.front_door") is False
        assert _should_skip_entity("light.my_office") is False

    def test_filter_entities_removes_junk(self):
        """filter_entities removes diagnostic/noisy entities."""
        entities = [
            {"entity_id": "switch.baby_berardi_timer", "name": "Baby Timer"},
            {"entity_id": "switch.hacs_pre_release", "name": "HACS Pre-release"},
            {"entity_id": "switch.office_sensor_motion_sensor_enabled", "name": "Motion"},
            {"entity_id": "switch.tz3210_j4pdtz9v_ts0001", "name": "TZ3210"},
            {"entity_id": "switch.my_rest_toddler_lock", "name": "Toddler Lock"},
        ]

        filtered = filter_entities(entities, "switch")

        entity_ids = [e["entity_id"] for e in filtered]
        assert "switch.baby_berardi_timer" in entity_ids
        assert "switch.my_rest_toddler_lock" in entity_ids
        assert "switch.hacs_pre_release" not in entity_ids
        assert "switch.tz3210_j4pdtz9v_ts0001" not in entity_ids

    def test_filter_entities_respects_cap(self):
        """filter_entities caps at domain max."""
        # Create more entities than the light cap (8)
        entities = [
            {"entity_id": f"light.room_{i}", "name": f"Room {i}"}
            for i in range(15)
        ]

        filtered = filter_entities(entities, "light")
        assert len(filtered) == 8


class TestSceneFiltering:
    """Test scene-specific filtering."""

    def test_keeps_functional_scenes(self):
        """Keeps scenes with functional types."""
        scenes = [
            {"entity_id": "scene.office_desk_read", "name": "Office Desk Read"},
            {"entity_id": "scene.basement_bright", "name": "Basement Bright"},
            {"entity_id": "scene.upstairs_relax", "name": "Upstairs Relax"},
            {"entity_id": "scene.middle_bathroom_nightlight", "name": "Bathroom Nightlight"},
        ]

        filtered = filter_scenes(scenes)
        assert len(filtered) == 4

    def test_removes_decorative_scenes(self):
        """Removes decorative/color theme scenes."""
        scenes = [
            {"entity_id": "scene.office_fan_tropical_twilight", "name": "Tropical Twilight"},
            {"entity_id": "scene.upstairs_spring_blossom", "name": "Spring Blossom"},
            {"entity_id": "scene.office_fan_arctic_aurora", "name": "Arctic Aurora"},
            {"entity_id": "scene.office_fan_savanna_sunset", "name": "Savanna Sunset"},
        ]

        filtered = filter_scenes(scenes)
        assert len(filtered) == 0

    def test_mixed_scenes(self):
        """Correctly filters a mix of functional and decorative scenes."""
        scenes = [
            {"entity_id": "scene.office_desk_read", "name": "Read"},
            {"entity_id": "scene.office_fan_tropical_twilight", "name": "Tropical"},
            {"entity_id": "scene.basement_bright", "name": "Bright"},
            {"entity_id": "scene.upstairs_spring_blossom", "name": "Blossom"},
            {"entity_id": "scene.office_desk_concentrate", "name": "Concentrate"},
        ]

        filtered = filter_scenes(scenes)
        entity_ids = [e["entity_id"] for e in filtered]

        assert "scene.office_desk_read" in entity_ids
        assert "scene.basement_bright" in entity_ids
        assert "scene.office_desk_concentrate" in entity_ids
        assert "scene.office_fan_tropical_twilight" not in entity_ids

    def test_scene_cap(self):
        """Respects scene cap."""
        scenes = [
            {"entity_id": f"scene.room_{i}_bright", "name": f"Room {i} Bright"}
            for i in range(20)
        ]

        filtered = filter_scenes(scenes)
        assert len(filtered) <= 15


# ---------------------------------------------------------------------------
# Template hydration tests
# ---------------------------------------------------------------------------

class TestTemplateHydration:
    """Test template token replacement."""

    def test_hydrate_name_token(self):
        """Replaces {{NAME}} with lowercased friendly name."""
        entity = {"entity_id": "light.basement", "name": "Basement"}
        result = hydrate_template("Turn on the {{NAME}} lights", entity)
        assert result == "Turn on the basement lights"

    def test_hydrate_name_preserves_rest_of_string(self):
        """Only replaces the token, not other text."""
        entity = {"entity_id": "switch.timer", "name": "Baby Timer"}
        result = hydrate_template("{{NAME}} on", entity)
        assert result == "baby timer on"

    def test_hydrate_room_token(self):
        """Replaces {{ROOM}} with parsed room from scene entity_id."""
        entity = {"entity_id": "scene.office_desk_read", "name": "Office Desk Read"}
        result = hydrate_template("Set the {{ROOM}} to {{SCENE_TYPE}}", entity)
        assert result == "Set the office desk to read"

    def test_hydrate_scene_type_token(self):
        """Replaces {{SCENE_TYPE}} with scene suffix."""
        entity = {"entity_id": "scene.basement_bright", "name": "Basement Bright"}
        result = hydrate_template("Activate the {{NAME}} scene", entity)
        assert result == "Activate the basement bright scene"

    def test_returns_none_when_name_missing(self):
        """Returns None when {{NAME}} token can't be resolved."""
        entity = {"entity_id": "light.something", "name": ""}
        result = hydrate_template("Turn on the {{NAME}}", entity)
        assert result is None

    def test_returns_none_when_room_unresolvable(self):
        """Returns None when {{ROOM}} token can't be resolved for non-scene."""
        entity = {"entity_id": "light.basement", "name": "Basement"}
        result = hydrate_template("Set the {{ROOM}} to relax", entity)
        assert result is None

    def test_returns_none_when_scene_type_unresolvable(self):
        """Returns None when {{SCENE_TYPE}} can't be parsed."""
        entity = {"entity_id": "light.basement", "name": "Basement"}
        result = hydrate_template("{{SCENE_TYPE}} mode", entity)
        assert result is None

    def test_no_tokens_passes_through(self):
        """Utterance with no tokens passes through unchanged."""
        entity = {"entity_id": "light.test", "name": "Test"}
        result = hydrate_template("Just a plain string", entity)
        assert result == "Just a plain string"


class TestSceneParsing:
    """Test scene entity_id parsing helpers."""

    def test_parse_scene_type(self):
        """Extracts scene type suffix."""
        assert _parse_scene_type("scene.office_desk_read") == "read"
        assert _parse_scene_type("scene.basement_bright") == "bright"
        assert _parse_scene_type("scene.my_office_dimmed") == "dimmed"
        assert _parse_scene_type("scene.upstairs_relax") == "relax"

    def test_parse_scene_type_non_scene(self):
        """Returns None for non-scene entities."""
        assert _parse_scene_type("light.basement") is None

    def test_parse_scene_room(self):
        """Extracts room/device name from scene entity_id."""
        assert _parse_scene_room("scene.office_desk_read") == "office desk"
        assert _parse_scene_room("scene.basement_bright") == "basement"
        assert _parse_scene_room("scene.my_office_dimmed") == "my office"
        assert _parse_scene_room("scene.middle_bathroom_nightlight") == "middle bathroom"

    def test_parse_scene_room_non_scene(self):
        """Returns None for non-scene entities."""
        assert _parse_scene_room("light.basement") is None


# ---------------------------------------------------------------------------
# Template loading tests
# ---------------------------------------------------------------------------

class TestTemplateLoading:
    """Test template loading and JSON merge."""

    def test_load_control_templates_builtin(self):
        """Loads built-in control templates."""
        templates = load_templates("control")

        assert "light" in templates
        assert "switch" in templates
        assert "scene" in templates
        assert "cover" in templates
        assert "lock" in templates

    def test_load_status_templates_builtin(self):
        """Loads built-in status templates."""
        templates = load_templates("status")

        assert "light" in templates
        assert "cover" in templates
        assert "lock" in templates
        assert "climate" in templates

    def test_json_merge_appends_not_replaces(self, tmp_path):
        """JSON override appends to built-in templates."""
        override_file = tmp_path / "additional_home_assistant_device_mappings.json"
        override_file.write_text(json.dumps({
            "control_templates": {
                "light": [
                    {
                        "action": "turn_on",
                        "utterances": ["Lights up in the {{NAME}}"],
                    }
                ]
            }
        }))

        with patch("utils.ha_training_data.Path") as mock_path_cls:
            mock_instance = MagicMock()
            mock_path_cls.return_value = mock_instance
            mock_instance.parent.parent.__truediv__.return_value = override_file

            templates = load_templates("control")

            light_templates = templates["light"]
            # Built-in has 2 entries (turn_on, turn_off), JSON adds 1 more
            assert len(light_templates) >= 3

            # Verify the override utterance was actually merged in
            all_utterances = [u for t in light_templates for u in t.get("utterances", [])]
            assert "Lights up in the {{NAME}}" in all_utterances

    def test_missing_json_file_uses_builtins(self):
        """Gracefully handles missing JSON override file."""
        templates = load_templates("control")

        # Should still return all built-in templates
        assert "light" in templates
        assert "cover" in templates
        assert len(templates["light"]) >= 2

    def test_templates_are_deep_copied(self):
        """Loading templates doesn't mutate module-level constants."""
        templates = load_templates("control")
        templates["light"].append({"action": "test", "utterances": ["test"]})

        # Original should be unchanged
        fresh = load_templates("control")
        assert not any(t.get("action") == "test" for t in fresh["light"])


# ---------------------------------------------------------------------------
# Example generation tests
# ---------------------------------------------------------------------------

class TestControlExampleGeneration:
    """Test generate_control_examples."""

    def test_generates_examples_from_light_controls(self, sample_light_controls):
        """Generates light control examples using light_controls data."""
        examples = generate_control_examples({}, sample_light_controls)

        assert len(examples) > 0

        # All examples should reference real entity IDs
        entity_ids = {ex.expected_parameters["entity_id"] for ex in examples}
        assert "light.basement" in entity_ids
        assert "light.my_office" in entity_ids

    def test_generates_scene_examples(self, sample_light_controls, sample_device_controls):
        """Generates scene activation examples."""
        examples = generate_control_examples(sample_device_controls, sample_light_controls)

        scene_examples = [
            ex for ex in examples
            if ex.expected_parameters.get("entity_id", "").startswith("scene.")
        ]
        assert len(scene_examples) > 0

        # Should only include functional scenes
        for ex in scene_examples:
            entity_id = ex.expected_parameters["entity_id"]
            scene_type = _parse_scene_type(entity_id)
            assert scene_type in FUNCTIONAL_SCENE_TYPES, f"Non-functional scene in examples: {entity_id}"

    def test_filters_junk_switches(self, sample_light_controls, sample_device_controls):
        """Filters out pre_release, sensor, and hex-name switches."""
        examples = generate_control_examples(sample_device_controls, sample_light_controls)

        switch_entity_ids = {
            ex.expected_parameters["entity_id"]
            for ex in examples
            if ex.expected_parameters.get("entity_id", "").startswith("switch.")
        }

        assert "switch.baby_berardi_timer" in switch_entity_ids
        assert "switch.hacs_pre_release" not in switch_entity_ids
        assert "switch.tz3210_j4pdtz9v_ts0001" not in switch_entity_ids
        assert "switch.office_sensor_motion_sensor_enabled" not in switch_entity_ids

    def test_first_example_is_primary(self, sample_light_controls):
        """First generated example is marked as primary."""
        examples = generate_control_examples({}, sample_light_controls)

        assert examples[0].is_primary is True
        # Only one primary
        assert sum(1 for ex in examples if ex.is_primary) == 1

    def test_includes_cover_examples(self, sample_light_controls, sample_device_controls):
        """Generates cover control examples when covers exist."""
        examples = generate_control_examples(sample_device_controls, sample_light_controls)

        cover_examples = [
            ex for ex in examples
            if ex.expected_parameters.get("entity_id", "").startswith("cover.")
        ]
        assert len(cover_examples) > 0

        # Should have both open and close actions
        actions = {ex.expected_parameters.get("action") for ex in cover_examples}
        assert "open_cover" in actions
        assert "close_cover" in actions

    def test_no_duplicate_utterances(self, sample_light_controls, sample_device_controls):
        """No duplicate utterances in generated examples."""
        examples = generate_control_examples(sample_device_controls, sample_light_controls)

        utterances = [ex.voice_command.lower() for ex in examples]
        assert len(utterances) == len(set(utterances))

    def test_control_examples_have_action(self, sample_light_controls, sample_device_controls):
        """Control examples include action parameter."""
        examples = generate_control_examples(sample_device_controls, sample_light_controls)

        for ex in examples:
            assert "action" in ex.expected_parameters, (
                f"Missing action for: {ex.voice_command}"
            )


class TestStatusExampleGeneration:
    """Test generate_status_examples."""

    def test_generates_light_status_examples(self, sample_light_controls):
        """Generates light status query examples."""
        examples = generate_status_examples({}, sample_light_controls)

        assert len(examples) > 0

        entity_ids = {ex.expected_parameters["entity_id"] for ex in examples}
        assert "light.basement" in entity_ids
        assert "light.my_office" in entity_ids

    def test_skips_scenes_for_status(self, sample_light_controls, sample_device_controls):
        """Doesn't generate status queries for scenes."""
        examples = generate_status_examples(sample_device_controls, sample_light_controls)

        scene_examples = [
            ex for ex in examples
            if ex.expected_parameters.get("entity_id", "").startswith("scene.")
        ]
        assert len(scene_examples) == 0

    def test_status_examples_have_entity_id_only(self, sample_light_controls, sample_device_controls):
        """Status examples only have entity_id parameter (no action)."""
        examples = generate_status_examples(sample_device_controls, sample_light_controls)

        for ex in examples:
            assert "entity_id" in ex.expected_parameters
            assert "action" not in ex.expected_parameters

    def test_first_example_is_primary(self, sample_light_controls):
        """First generated example is marked as primary."""
        examples = generate_status_examples({}, sample_light_controls)

        assert examples[0].is_primary is True
        assert sum(1 for ex in examples if ex.is_primary) == 1

    def test_no_duplicate_utterances(self, sample_light_controls, sample_device_controls):
        """No duplicate utterances in status examples."""
        examples = generate_status_examples(sample_device_controls, sample_light_controls)

        utterances = [ex.voice_command.lower() for ex in examples]
        assert len(utterances) == len(set(utterances))


# ---------------------------------------------------------------------------
# Caching tests
# ---------------------------------------------------------------------------

class TestCaching:
    """Test module-level caching behavior."""

    @patch("agents.home_assistant_agent.HomeAssistantAgent")
    def test_second_call_uses_cache(self, mock_agent_cls):
        """Second call to get_ha_training_data returns cached data."""
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()
        mock_agent.get_context_data.return_value = {
            "device_controls": {"light": []},
            "light_controls": {},
            "last_error": None,
        }
        mock_agent_cls.return_value = mock_agent

        # First call fetches
        result1 = get_ha_training_data()
        assert result1 is not None

        # Second call returns cached
        result2 = get_ha_training_data()
        assert result2 is result1

        # Agent was only instantiated once
        assert mock_agent_cls.call_count == 1

    @patch("agents.home_assistant_agent.HomeAssistantAgent")
    def test_clear_cache_allows_refetch(self, mock_agent_cls):
        """Clearing cache allows fresh fetch."""
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()
        mock_agent.get_context_data.return_value = {
            "device_controls": {"light": []},
            "light_controls": {},
            "last_error": None,
        }
        mock_agent_cls.return_value = mock_agent

        get_ha_training_data()
        clear_ha_training_cache()
        get_ha_training_data()

        # Agent instantiated twice (once per fetch)
        assert mock_agent_cls.call_count == 2


# ---------------------------------------------------------------------------
# Fallback tests
# ---------------------------------------------------------------------------

class TestFallback:
    """Test fallback behavior when HA is unreachable."""

    @patch("agents.home_assistant_agent.HomeAssistantAgent")
    def test_returns_none_on_agent_error(self, mock_agent_cls):
        """Returns None when agent run() raises."""
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=ConnectionRefusedError("HA down"))
        mock_agent_cls.return_value = mock_agent

        result = get_ha_training_data()
        assert result is None

    @patch("agents.home_assistant_agent.HomeAssistantAgent")
    def test_returns_none_on_last_error(self, mock_agent_cls):
        """Returns None when agent reports last_error."""
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock()
        mock_agent.get_context_data.return_value = {
            "device_controls": {},
            "light_controls": {},
            "last_error": "Connection timeout",
        }
        mock_agent_cls.return_value = mock_agent

        result = get_ha_training_data()
        assert result is None

    @patch("agents.home_assistant_agent.HomeAssistantAgent")
    def test_returns_none_on_import_error(self, mock_agent_cls):
        """Returns None when HomeAssistantAgent raises during run."""
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=ImportError("websockets not installed"))
        mock_agent_cls.return_value = mock_agent

        result = get_ha_training_data()
        assert result is None

    @patch("agents.home_assistant_agent.HomeAssistantAgent")
    def test_caches_none_result(self, mock_agent_cls):
        """Caches None result (doesn't re-fetch on failure)."""
        mock_agent = MagicMock()
        mock_agent.run = AsyncMock(side_effect=ConnectionRefusedError("HA down"))
        mock_agent_cls.return_value = mock_agent

        result1 = get_ha_training_data()
        result2 = get_ha_training_data()

        assert result1 is None
        assert result2 is None
        # Only tried once
        assert mock_agent_cls.call_count == 1


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------

class TestEndToEnd:
    """Integration tests combining filtering + hydration + generation."""

    def test_realistic_control_example_count(self, sample_light_controls, sample_device_controls):
        """Generates a reasonable number of control examples."""
        examples = generate_control_examples(sample_device_controls, sample_light_controls)

        # Should generate meaningful number of examples
        # 6 lights * ~7 utterances + switches + scenes + covers = substantial
        assert len(examples) >= 20
        assert len(examples) <= 200

    def test_realistic_status_example_count(self, sample_light_controls, sample_device_controls):
        """Generates a reasonable number of status examples."""
        examples = generate_status_examples(sample_device_controls, sample_light_controls)

        assert len(examples) >= 10
        assert len(examples) <= 100

    def test_all_entity_ids_are_real(self, sample_light_controls, sample_device_controls):
        """All generated entity IDs come from the input data."""
        all_entity_ids = set()
        for info in sample_light_controls.values():
            all_entity_ids.add(info["entity_id"])
        for entities in sample_device_controls.values():
            for entity in entities:
                all_entity_ids.add(entity["entity_id"])

        examples = generate_control_examples(sample_device_controls, sample_light_controls)

        for ex in examples:
            entity_id = ex.expected_parameters["entity_id"]
            assert entity_id in all_entity_ids, (
                f"Generated example uses unknown entity_id: {entity_id}"
            )

    def test_empty_data_returns_empty_list(self):
        """Empty device_controls and light_controls returns empty list."""
        examples = generate_control_examples({}, {})
        assert examples == []

        examples = generate_status_examples({}, {})
        assert examples == []
