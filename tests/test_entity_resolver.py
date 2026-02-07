"""
Unit tests for entity_resolver module.

Tests fuzzy entity resolution: ID similarity scoring, name overlap scoring,
entity registry fetching/caching, and end-to-end resolution.
"""

import pytest
from unittest.mock import patch, MagicMock

import httpx

from utils.entity_resolver import (
    EntityInfo,
    _compute_id_score,
    _compute_name_score,
    _get_entity_registry,
    clear_entity_registry_cache,
    resolve_entity_id,
    MATCH_THRESHOLD,
)


@pytest.fixture(autouse=True)
def reset_cache():
    """Clear entity registry cache before and after each test."""
    clear_entity_registry_cache()
    yield
    clear_entity_registry_cache()


# ---------- _compute_id_score tests ----------


class TestComputeIdScore:
    """Test entity ID string similarity scoring."""

    def test_identical_names(self):
        assert _compute_id_score("my_office", "my_office") == 1.0

    def test_similar_names(self):
        score = _compute_id_score("office", "my_office")
        assert score > 0.6

    def test_dissimilar_names(self):
        score = _compute_id_score("office", "basement")
        assert score < 0.4

    def test_empty_strings(self):
        assert _compute_id_score("", "") == 1.0  # SequenceMatcher gives 1.0 for equal strings

    def test_partial_overlap(self):
        score = _compute_id_score("garage_door", "garage")
        assert score > 0.5

    def test_completely_different(self):
        score = _compute_id_score("abc", "xyz")
        assert score == 0.0


# ---------- _compute_name_score tests ----------


class TestComputeNameScore:
    """Test voice command vs friendly name word overlap scoring."""

    def test_full_overlap(self):
        score = _compute_name_score("office light", "Office")
        assert score == 1.0

    def test_partial_overlap(self):
        score = _compute_name_score("turn on the office", "My Office")
        # "office" matches, "my" is in the name but not in filtered command
        # name_words = {"my", "office"}, command_words = {"office"} (stop words removed)
        # overlap = {"office"}, score = 1/2 = 0.5
        assert score == 0.5

    def test_stop_words_filtered(self):
        """Stop words in voice command are filtered out."""
        score = _compute_name_score("Is the office light on?", "Office")
        # command_words after stop filter: {"office", "light", "on?"} -> but "on?" stays as it has ?
        # Actually "on?" won't match "on" exactly, let's check
        # name_words = {"office"}, overlap should include "office"
        assert score == 1.0

    def test_no_overlap(self):
        score = _compute_name_score("turn on the bedroom fan", "Garage Door")
        assert score == 0.0

    def test_empty_voice_command(self):
        score = _compute_name_score("", "My Office")
        assert score == 0.0

    def test_empty_friendly_name(self):
        score = _compute_name_score("office light", "")
        assert score == 0.0

    def test_both_empty(self):
        score = _compute_name_score("", "")
        assert score == 0.0

    def test_case_insensitive(self):
        score = _compute_name_score("OFFICE LIGHT", "office")
        assert score == 1.0

    def test_multi_word_name_full_match(self):
        score = _compute_name_score("check the garage door", "Garage Door")
        # command: {"garage", "door"} (stop words removed)
        # name: {"garage", "door"}, overlap = 2/2 = 1.0
        assert score == 1.0


# ---------- _get_entity_registry tests ----------


class TestGetEntityRegistry:
    """Test entity registry fetching and caching."""

    @patch("utils.entity_resolver.get_secret_value")
    @patch("utils.entity_resolver.httpx.get")
    def test_fetches_and_caches(self, mock_get, mock_secret):
        mock_secret.side_effect = lambda key, _: {
            "HOME_ASSISTANT_REST_URL": "http://ha:8123",
            "HOME_ASSISTANT_API_KEY": "test-token",
        }[key]

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"entity_id": "light.my_office", "attributes": {"friendly_name": "My Office"}},
            {"entity_id": "light.basement", "attributes": {"friendly_name": "Basement"}},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = _get_entity_registry()

        assert len(result) == 2
        assert result[0].entity_id == "light.my_office"
        assert result[0].friendly_name == "My Office"

        # Second call should use cache (no additional HTTP call)
        result2 = _get_entity_registry()
        assert len(result2) == 2
        mock_get.assert_called_once()

    @patch("utils.entity_resolver.get_secret_value")
    @patch("utils.entity_resolver.httpx.get")
    def test_http_error_returns_empty(self, mock_get, mock_secret):
        mock_secret.side_effect = lambda key, _: {
            "HOME_ASSISTANT_REST_URL": "http://ha:8123",
            "HOME_ASSISTANT_API_KEY": "test-token",
        }[key]

        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found", request=MagicMock(), response=MagicMock(status_code=404)
        )
        mock_get.return_value = mock_response

        result = _get_entity_registry()
        assert result == []

    @patch("utils.entity_resolver.get_secret_value")
    @patch("utils.entity_resolver.httpx.get")
    def test_connection_error_returns_empty(self, mock_get, mock_secret):
        mock_secret.side_effect = lambda key, _: {
            "HOME_ASSISTANT_REST_URL": "http://ha:8123",
            "HOME_ASSISTANT_API_KEY": "test-token",
        }[key]

        mock_get.side_effect = httpx.ConnectError("Connection refused")

        result = _get_entity_registry()
        assert result == []

    @patch("utils.entity_resolver.get_secret_value")
    def test_missing_credentials_returns_empty(self, mock_secret):
        mock_secret.side_effect = ValueError("Secret not found")

        result = _get_entity_registry()
        assert result == []

    @patch("utils.entity_resolver.get_secret_value")
    @patch("utils.entity_resolver.httpx.get")
    def test_clear_cache_resets(self, mock_get, mock_secret):
        mock_secret.side_effect = lambda key, _: {
            "HOME_ASSISTANT_REST_URL": "http://ha:8123",
            "HOME_ASSISTANT_API_KEY": "test-token",
        }[key]

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"entity_id": "light.test", "attributes": {"friendly_name": "Test"}},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        _get_entity_registry()
        assert mock_get.call_count == 1

        clear_entity_registry_cache()

        _get_entity_registry()
        assert mock_get.call_count == 2

    @patch("utils.entity_resolver.get_secret_value")
    @patch("utils.entity_resolver.httpx.get")
    def test_handles_missing_friendly_name(self, mock_get, mock_secret):
        """Entities without friendly_name get empty string."""
        mock_secret.side_effect = lambda key, _: {
            "HOME_ASSISTANT_REST_URL": "http://ha:8123",
            "HOME_ASSISTANT_API_KEY": "test-token",
        }[key]

        mock_response = MagicMock()
        mock_response.json.return_value = [
            {"entity_id": "sensor.temp", "attributes": {}},
        ]
        mock_response.raise_for_status = MagicMock()
        mock_get.return_value = mock_response

        result = _get_entity_registry()
        assert len(result) == 1
        assert result[0].friendly_name == ""


# ---------- resolve_entity_id tests ----------


SAMPLE_REGISTRY = [
    EntityInfo("light.my_office", "My Office"),
    EntityInfo("light.basement", "Basement"),
    EntityInfo("light.office_desk", "Office Desk"),
    EntityInfo("light.office_fan", "Office Fan"),
    EntityInfo("light.upstairs", "Upstairs"),
    EntityInfo("light.middle_bathroom", "Middle Bathroom"),
    EntityInfo("cover.garage_door", "Garage Door"),
    EntityInfo("lock.front_door", "Front Door"),
    EntityInfo("climate.thermostat", "Thermostat"),
    EntityInfo("switch.baby_berardi_timer", "Baby Berardi Timer"),
    EntityInfo("scene.office_desk_read", "Office Desk Read"),
]


class TestResolveEntityId:
    """Test end-to-end entity resolution."""

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_exact_match_passthrough(self, mock_reg):
        result = resolve_entity_id("light.my_office", "turn on my office lights")
        assert result == "light.my_office"

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_id_similarity_resolves(self, mock_reg):
        """light.office should resolve to light.my_office via ID similarity."""
        result = resolve_entity_id("light.office", "")
        # "office" vs "my_office" has good SequenceMatcher ratio
        assert result in ("light.my_office", "light.office_desk", "light.office_fan")

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_name_overlap_resolves(self, mock_reg):
        """Voice command word overlap helps resolve ambiguous entity."""
        result = resolve_entity_id("light.office", "Is the office light on?")
        # "office" and "light" overlap with multiple entities, but
        # "My Office" has full name overlap with voice command word "office"
        assert result in ("light.my_office", "light.office_desk", "light.office_fan")

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_domain_filtering(self, mock_reg):
        """light.garage should not match cover.garage_door."""
        result = resolve_entity_id("light.garage", "open the garage")
        # No light entities have "garage" — should stay as-is or match poorly
        assert result.startswith("light.")

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_below_threshold_returns_original(self, mock_reg):
        """Very dissimilar entity stays as-is."""
        result = resolve_entity_id("light.zzz_nonexistent", "")
        assert result == "light.zzz_nonexistent"

    @patch("utils.entity_resolver._get_entity_registry", return_value=[])
    def test_empty_registry_returns_original(self, mock_reg):
        result = resolve_entity_id("light.office", "turn on office")
        assert result == "light.office"

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_no_dot_in_entity_returns_original(self, mock_reg):
        result = resolve_entity_id("invalid_format", "something")
        assert result == "invalid_format"

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_unknown_domain_returns_original(self, mock_reg):
        """Entity with domain that has no candidates returns original."""
        result = resolve_entity_id("water_heater.test", "check the water heater")
        assert result == "water_heater.test"


# ---------- Real-world scenario tests ----------


class TestRealWorldScenarios:
    """Test realistic LLM mistake scenarios."""

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_office_light_status(self, mock_reg):
        """'Is the office light on?' with light.office_desk → light.my_office or desk."""
        # The LLM returned "light.office_desk" but it exists, so exact match
        result = resolve_entity_id("light.office_desk", "Is the office light on?")
        assert result == "light.office_desk"  # Exact match

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_office_light_invented_id(self, mock_reg):
        """LLM invents light.office (doesn't exist) → should fuzzy match."""
        result = resolve_entity_id("light.office", "Is the office light on?")
        # Should match one of the office-related lights
        assert result.startswith("light.")
        assert result != "light.office"  # Should be resolved to something real

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_garage_door_cover(self, mock_reg):
        """cover.garage → cover.garage_door via ID similarity."""
        result = resolve_entity_id("cover.garage", "open the garage")
        assert result == "cover.garage_door"

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_bathroom_light(self, mock_reg):
        """light.bathroom → light.middle_bathroom via name overlap."""
        result = resolve_entity_id("light.bathroom", "Is the bathroom light on?")
        assert result == "light.middle_bathroom"

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_thermostat_exact(self, mock_reg):
        """climate.thermostat exists exactly."""
        result = resolve_entity_id("climate.thermostat", "What's the thermostat set to?")
        assert result == "climate.thermostat"

    @patch("utils.entity_resolver._get_entity_registry", return_value=SAMPLE_REGISTRY)
    def test_front_door_lock(self, mock_reg):
        """lock.front → lock.front_door via ID similarity."""
        result = resolve_entity_id("lock.front", "Is the front door locked?")
        assert result == "lock.front_door"
