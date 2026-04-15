"""Tests for RoutineCommand — voice routines (good morning, good night, etc.)."""

from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from core.command_response import CommandResponse
from jarvis_command_sdk import PreRouteResult
from core.request_information import RequestInformation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request_info(voice_command: str = "good morning") -> RequestInformation:
    return RequestInformation(
        voice_command=voice_command,
        conversation_id="test-conv-123",
        is_validation_response=False,
    )


def _weather_response() -> CommandResponse:
    return CommandResponse.success_response(
        context_data={
            "city": "Chicago",
            "temperature": 72,
            "description": "clear sky",
            "humidity": 45,
            "unit_system": "imperial",
            "weather_type": "current",
        }
    )


def _calendar_response() -> CommandResponse:
    return CommandResponse.success_response(
        context_data={
            "message": "You have a 9am meeting with the design team.",
            "events": [{"title": "Design team sync", "time": "9:00 AM"}],
        }
    )


def _device_response() -> CommandResponse:
    return CommandResponse.success_response(
        context_data={"message": "Turned on Downstairs lights."}
    )


def _failing_response() -> CommandResponse:
    return CommandResponse.error_response(error_details="Service unavailable")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def routine_cmd():
    """Import and instantiate RoutineCommand."""
    from commands.routine_command import RoutineCommand
    return RoutineCommand()


@pytest.fixture
def custom_routines():
    """Sample custom routines config as a JSON-parsed dict."""
    return {
        "good_morning": {
            "trigger_phrases": ["good morning", "morning routine", "start my day"],
            "steps": [
                {"command": "control_device", "args": {"floor": "Downstairs", "action": "turn_on"}, "label": "lights"},
                {"command": "get_weather", "args": {"resolved_datetimes": ["today"]}, "label": "weather"},
                {"command": "get_calendar_events", "args": {"resolved_datetimes": ["today"]}, "label": "calendar"},
            ],
            "response_instruction": "Give a cheerful morning briefing with weather and calendar highlights.",
        },
        "good_night": {
            "trigger_phrases": ["good night", "bedtime", "going to bed"],
            "steps": [
                {"command": "control_device", "args": {"floor": "Downstairs", "action": "turn_off"}, "label": "lights_down"},
                {"command": "control_device", "args": {"floor": "Upstairs", "action": "turn_off"}, "label": "lights_up"},
                {"command": "get_calendar_events", "args": {"resolved_datetimes": ["tomorrow"]}, "label": "tomorrow"},
            ],
            "response_instruction": "Brief goodnight with tomorrow's first appointment if any.",
        },
    }


# ===================================================================
# Pre-route matching tests (critical path — no LLM fallback)
# ===================================================================

class TestPreRouteMatching:
    """Trigger phrase matching is the critical path — test extensively."""

    def _pre_route(self, cmd, text: str, routines: dict | None = None) -> PreRouteResult | None:
        with patch("commands.routine_command._load_routines") as mock_load:
            mock_load.return_value = routines or cmd._default_routines()
            return cmd.pre_route(text)

    # --- Exact match ---

    def test_exact_match(self, routine_cmd):
        result = self._pre_route(routine_cmd, "good morning")
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    def test_exact_match_case_insensitive(self, routine_cmd):
        result = self._pre_route(routine_cmd, "Good Morning")
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    def test_exact_match_with_whitespace(self, routine_cmd):
        result = self._pre_route(routine_cmd, "  good morning  ")
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    def test_exact_match_good_night(self, routine_cmd):
        result = self._pre_route(routine_cmd, "good night")
        assert result is not None
        assert result.arguments["routine_name"] == "good_night"

    def test_exact_match_bedtime(self, routine_cmd):
        result = self._pre_route(routine_cmd, "bedtime")
        assert result is not None
        assert result.arguments["routine_name"] == "good_night"

    # --- Substring match (phrase in text) ---

    def test_substring_good_morning_jarvis(self, routine_cmd):
        result = self._pre_route(routine_cmd, "good morning jarvis")
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    def test_substring_hey_jarvis_good_morning(self, routine_cmd):
        result = self._pre_route(routine_cmd, "hey jarvis good morning")
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    def test_substring_going_to_bed(self, routine_cmd):
        result = self._pre_route(routine_cmd, "hey jarvis I'm going to bed")
        assert result is not None
        assert result.arguments["routine_name"] == "good_night"

    # --- Reversed substring (text in phrase) ---

    def test_reversed_substring_morning(self, routine_cmd):
        """Short utterance 'morning' should match 'morning routine' trigger."""
        result = self._pre_route(routine_cmd, "morning")
        # "morning" is a substring of "morning routine"
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    # --- No match ---

    def test_no_match_weather(self, routine_cmd):
        result = self._pre_route(routine_cmd, "what's the weather")
        assert result is None

    def test_no_match_music(self, routine_cmd):
        result = self._pre_route(routine_cmd, "play some music")
        assert result is None

    def test_no_match_good_afternoon(self, routine_cmd):
        """'good afternoon' should NOT match 'good morning'."""
        result = self._pre_route(routine_cmd, "good afternoon")
        assert result is None

    def test_no_match_empty(self, routine_cmd):
        result = self._pre_route(routine_cmd, "")
        assert result is None

    # --- Custom routines from config ---

    def test_custom_routine(self, routine_cmd, custom_routines):
        custom_routines["workout"] = {
            "trigger_phrases": ["workout time", "let's exercise"],
            "steps": [],
            "response_instruction": "Motivate!",
        }
        result = self._pre_route(routine_cmd, "workout time", custom_routines)
        assert result is not None
        assert result.arguments["routine_name"] == "workout"

    def test_custom_routine_overrides_default(self, routine_cmd, custom_routines):
        """Custom config with same key overrides defaults."""
        custom_routines["good_morning"]["trigger_phrases"] = ["wake up"]
        result = self._pre_route(routine_cmd, "wake up", custom_routines)
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    # --- Multiple routines: correct one wins ---

    def test_correct_routine_wins(self, routine_cmd, custom_routines):
        result = self._pre_route(routine_cmd, "bedtime", custom_routines)
        assert result is not None
        assert result.arguments["routine_name"] == "good_night"

    # --- Keyword overlap ---

    def test_keyword_overlap_start_my_day(self, routine_cmd):
        """'start my day' is a trigger phrase — exact match."""
        result = self._pre_route(routine_cmd, "start my day")
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"

    def test_keyword_overlap_time_for_bed(self, routine_cmd):
        """'time for bed' should match 'bedtime' via keyword overlap."""
        result = self._pre_route(routine_cmd, "time for bed")
        assert result is not None
        assert result.arguments["routine_name"] == "good_night"


# ===================================================================
# Sub-command execution tests
# ===================================================================

class TestSubCommandExecution:
    """Test that run() executes sub-commands and collects results."""

    def _run_routine(
        self,
        routine_cmd,
        routine_name: str,
        routines: dict,
        command_mocks: dict[str, MagicMock],
        chat_text_return: str | None = "Good morning! It's 72 and sunny.",
    ) -> CommandResponse:
        """Helper to run a routine with mocked sub-commands and LLM."""
        mock_discovery = MagicMock()

        def get_command(name):
            return command_mocks.get(name)

        mock_discovery.get_command.side_effect = get_command

        with (
            patch("commands.routine_command._load_routines", return_value=routines),
            patch("commands.routine_command.get_command_discovery_service", return_value=mock_discovery),
            patch("commands.routine_command.JarvisCommandCenterClient") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.chat_text.return_value = chat_text_return
            mock_client_cls.return_value = mock_client

            request_info = _make_request_info()
            return routine_cmd.run(request_info, routine_name=routine_name)

    def test_all_steps_succeed(self, routine_cmd, custom_routines):
        """All sub-commands execute, results aggregated, LLM composes response."""
        weather_cmd = MagicMock()
        weather_cmd.execute.return_value = _weather_response()
        calendar_cmd = MagicMock()
        calendar_cmd.execute.return_value = _calendar_response()
        device_cmd = MagicMock()
        device_cmd.execute.return_value = _device_response()

        result = self._run_routine(
            routine_cmd,
            "good_morning",
            custom_routines,
            {"control_device": device_cmd, "get_weather": weather_cmd, "get_calendar_events": calendar_cmd},
        )

        assert result.success is True
        assert "message" in result.context_data
        # LLM was called
        assert result.context_data["message"] == "Good morning! It's 72 and sunny."
        # Each sub-command was called once
        device_cmd.execute.assert_called_once()
        weather_cmd.execute.assert_called_once()
        calendar_cmd.execute.assert_called_once()

    def test_one_step_fails_others_continue(self, routine_cmd, custom_routines):
        """If weather fails, calendar and lights still execute."""
        weather_cmd = MagicMock()
        weather_cmd.execute.side_effect = Exception("API timeout")
        calendar_cmd = MagicMock()
        calendar_cmd.execute.return_value = _calendar_response()
        device_cmd = MagicMock()
        device_cmd.execute.return_value = _device_response()

        result = self._run_routine(
            routine_cmd,
            "good_morning",
            custom_routines,
            {"control_device": device_cmd, "get_weather": weather_cmd, "get_calendar_events": calendar_cmd},
        )

        assert result.success is True
        calendar_cmd.execute.assert_called_once()
        device_cmd.execute.assert_called_once()

    def test_all_steps_fail(self, routine_cmd, custom_routines):
        """When every sub-command fails, return error response."""
        weather_cmd = MagicMock()
        weather_cmd.execute.side_effect = Exception("fail")
        calendar_cmd = MagicMock()
        calendar_cmd.execute.side_effect = Exception("fail")
        device_cmd = MagicMock()
        device_cmd.execute.side_effect = Exception("fail")

        result = self._run_routine(
            routine_cmd,
            "good_morning",
            custom_routines,
            {"control_device": device_cmd, "get_weather": weather_cmd, "get_calendar_events": calendar_cmd},
        )

        assert result.success is False

    def test_missing_command_skipped(self, routine_cmd, custom_routines):
        """Steps referencing unknown commands are skipped gracefully."""
        calendar_cmd = MagicMock()
        calendar_cmd.execute.return_value = _calendar_response()
        device_cmd = MagicMock()
        device_cmd.execute.return_value = _device_response()

        # get_weather not in command_mocks — simulates uninstalled command
        result = self._run_routine(
            routine_cmd,
            "good_morning",
            custom_routines,
            {"control_device": device_cmd, "get_calendar_events": calendar_cmd},
        )

        assert result.success is True
        calendar_cmd.execute.assert_called_once()

    def test_missing_secrets_step_skipped(self, routine_cmd, custom_routines):
        """Steps raising MissingSecretsError are caught and skipped."""
        from jarvis_command_sdk import MissingSecretsError

        weather_cmd = MagicMock()
        weather_cmd.execute.side_effect = MissingSecretsError(["OPENWEATHER_API_KEY"])
        calendar_cmd = MagicMock()
        calendar_cmd.execute.return_value = _calendar_response()
        device_cmd = MagicMock()
        device_cmd.execute.return_value = _device_response()

        result = self._run_routine(
            routine_cmd,
            "good_morning",
            custom_routines,
            {"control_device": device_cmd, "get_weather": weather_cmd, "get_calendar_events": calendar_cmd},
        )

        assert result.success is True
        calendar_cmd.execute.assert_called_once()

    def test_step_args_passed_correctly(self, routine_cmd, custom_routines):
        """Verify sub-commands receive the args from the step definition."""
        device_cmd = MagicMock()
        device_cmd.execute.return_value = _device_response()
        weather_cmd = MagicMock()
        weather_cmd.execute.return_value = _weather_response()
        calendar_cmd = MagicMock()
        calendar_cmd.execute.return_value = _calendar_response()

        self._run_routine(
            routine_cmd,
            "good_morning",
            custom_routines,
            {"control_device": device_cmd, "get_weather": weather_cmd, "get_calendar_events": calendar_cmd},
        )

        # Device command should get floor + action from step args
        call_kwargs = device_cmd.execute.call_args
        assert call_kwargs[1]["floor"] == "Downstairs"
        assert call_kwargs[1]["action"] == "turn_on"

        # Calendar should get resolved_datetimes (resolved from "today" to YYYY-MM-DD)
        cal_kwargs = calendar_cmd.execute.call_args
        resolved_dates = cal_kwargs[1]["resolved_datetimes"]
        assert len(resolved_dates) == 1
        # Should be an actual date string, not the keyword "today"
        from datetime import datetime
        datetime.strptime(resolved_dates[0], "%Y-%m-%d")  # raises if not valid date


# ===================================================================
# LLM composition tests
# ===================================================================

class TestLLMComposition:
    """Test _compose_response and fallback behavior."""

    def test_happy_path_chat_text(self, routine_cmd):
        """chat_text returns composed text."""
        with (
            patch("commands.routine_command.get_command_center_url", return_value="http://localhost:7703"),
            patch("commands.routine_command.JarvisCommandCenterClient") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_client.chat_text.return_value = "Good morning! It's sunny."
            mock_cls.return_value = mock_client

            result = routine_cmd._compose_response(
                results={"weather": {"temperature": 72}},
                errors={"lights": "Service unavailable"},
                instruction="Give a cheerful briefing.",
            )

            assert result == "Good morning! It's sunny."
            # Verify prompt includes results and instruction
            prompt = mock_client.chat_text.call_args[0][0]
            assert "cheerful briefing" in prompt
            assert "weather" in prompt

    def test_fallback_when_chat_text_fails(self, routine_cmd):
        """When chat_text returns None, fall back to concatenation."""
        with (
            patch("commands.routine_command.get_command_center_url", return_value="http://localhost:7703"),
            patch("commands.routine_command.JarvisCommandCenterClient") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_client.chat_text.return_value = None
            mock_cls.return_value = mock_client

            result = routine_cmd._compose_response(
                results={
                    "weather": {"message": "72 and sunny"},
                    "calendar": {"message": "You have a meeting at 9"},
                },
                errors={},
                instruction="Brief morning.",
            )

            assert "72 and sunny" in result
            assert "meeting at 9" in result

    def test_fallback_extracts_message_from_nested(self, routine_cmd):
        """Fallback pulls 'message' from context_data dicts."""
        with (
            patch("commands.routine_command.get_command_center_url", return_value="http://localhost:7703"),
            patch("commands.routine_command.JarvisCommandCenterClient") as mock_cls,
        ):
            mock_client = MagicMock()
            mock_client.chat_text.return_value = None
            mock_cls.return_value = mock_client

            result = routine_cmd._compose_response(
                results={"lights": {"message": "Lights turned on"}},
                errors={},
                instruction="Brief.",
            )

            assert "Lights turned on" in result


# ===================================================================
# Config handling tests
# ===================================================================

class TestRoutineStorage:
    """Test DB loading and defaults."""

    def test_empty_db_uses_defaults(self, routine_cmd):
        """When DB has no routines, defaults are seeded and used."""
        with patch("commands.routine_command._load_routines") as mock_load:
            mock_load.return_value = routine_cmd._default_routines()
            result = routine_cmd.pre_route("good morning")
            assert result is not None

    def test_empty_steps_returns_error(self, routine_cmd):
        """Routine with empty steps list returns error."""
        routines = {
            "empty": {
                "trigger_phrases": ["do nothing"],
                "steps": [],
                "response_instruction": "Say something.",
            }
        }
        with (
            patch("commands.routine_command._load_routines", return_value=routines),
            patch("commands.routine_command.get_command_discovery_service"),
            patch("commands.routine_command.JarvisCommandCenterClient"),
        ):
            result = routine_cmd.run(_make_request_info(), routine_name="empty")
            assert result.success is False

    def test_unknown_routine_name_returns_error(self, routine_cmd):
        """Passing a routine name that doesn't exist returns error."""
        with patch("commands.routine_command._load_routines", return_value={}):
            result = routine_cmd.run(_make_request_info(), routine_name="nonexistent")
            assert result.success is False


# ===================================================================
# Command metadata tests
# ===================================================================

class TestCommandMetadata:
    """Verify command properties for discovery and schema."""

    def test_command_name(self, routine_cmd):
        assert routine_cmd.command_name == "routine"

    def test_has_keywords(self, routine_cmd):
        assert "routine" in routine_cmd.keywords
        assert "good morning" in routine_cmd.keywords

    def test_has_parameters(self, routine_cmd):
        param_names = [p.name for p in routine_cmd.parameters]
        assert "routine_name" in param_names

    def test_no_required_secrets(self, routine_cmd):
        assert routine_cmd.required_secrets == []

    def test_keywords_include_briefing_terms(self, routine_cmd):
        assert "briefing" in routine_cmd.keywords
        assert "daily briefing" in routine_cmd.keywords
        assert "morning briefing" in routine_cmd.keywords
        assert "catch me up" in routine_cmd.keywords


# ===================================================================
# Briefing type tests
# ===================================================================

class TestBriefingType:
    """Verify that type='briefing' uses medium length and narrative prompt."""

    def _run_with_type(
        self,
        routine_cmd,
        routine_def: dict,
        chat_text_return: str = "Here's your briefing...",
    ) -> tuple:
        """Run a routine and capture the prompt sent to chat_text."""
        mock_discovery = MagicMock()
        mock_cmd = MagicMock()
        mock_cmd.execute.return_value = CommandResponse.success_response(
            context_data={"message": "test data"}
        )
        mock_discovery.get_command.return_value = mock_cmd

        routines = {"test_routine": routine_def}

        with (
            patch("commands.routine_command._load_routines", return_value=routines),
            patch("commands.routine_command.get_command_discovery_service", return_value=mock_discovery),
            patch("commands.routine_command.JarvisCommandCenterClient") as mock_client_cls,
        ):
            mock_client = MagicMock()
            mock_client.chat_text.return_value = chat_text_return
            mock_client_cls.return_value = mock_client

            result = routine_cmd.run(_make_request_info(), routine_name="test_routine")
            prompt = mock_client.chat_text.call_args[0][0]
            return result, prompt

    def test_briefing_uses_medium_length(self, routine_cmd):
        routine_def = {
            "type": "briefing",
            "trigger_phrases": ["test"],
            "steps": [{"command": "get_news", "args": {}, "label": "news"}],
            "response_instruction": "Deliver a briefing.",
        }
        _, prompt = self._run_with_type(routine_cmd, routine_def)
        assert "6-10 spoken sentences" in prompt

    def test_routine_uses_short_length(self, routine_cmd):
        routine_def = {
            "type": "routine",
            "trigger_phrases": ["test"],
            "steps": [{"command": "get_weather", "args": {}, "label": "weather"}],
            "response_instruction": "Morning update.",
        }
        _, prompt = self._run_with_type(routine_cmd, routine_def)
        assert "2-4 spoken sentences" in prompt

    def test_default_type_is_routine(self, routine_cmd):
        """Routines without explicit type default to 'routine' (short)."""
        routine_def = {
            "trigger_phrases": ["test"],
            "steps": [{"command": "get_weather", "args": {}, "label": "weather"}],
            "response_instruction": "Update.",
        }
        _, prompt = self._run_with_type(routine_cmd, routine_def)
        assert "2-4 spoken sentences" in prompt

    def test_explicit_response_length_overrides_type(self, routine_cmd):
        """Explicit response_length in definition overrides the type default."""
        routine_def = {
            "type": "briefing",
            "response_length": "long",
            "trigger_phrases": ["test"],
            "steps": [{"command": "get_news", "args": {}, "label": "news"}],
            "response_instruction": "Detailed briefing.",
        }
        _, prompt = self._run_with_type(routine_cmd, routine_def)
        assert "detailed paragraph" in prompt.lower()


# ===================================================================
# Briefing pre-route tests
# ===================================================================

class TestBriefingPreRoute:
    """Verify briefing trigger phrases route correctly."""

    def _pre_route(self, cmd, text: str) -> PreRouteResult | None:
        with patch("commands.routine_command._load_routines") as mock_load:
            mock_load.return_value = cmd._default_routines()
            return cmd.pre_route(text)

    def test_morning_briefing_matches(self, routine_cmd):
        result = self._pre_route(routine_cmd, "morning briefing")
        assert result is not None
        assert result.arguments["routine_name"] == "morning_briefing"

    def test_daily_briefing_matches(self, routine_cmd):
        result = self._pre_route(routine_cmd, "daily briefing")
        assert result is not None
        assert result.arguments["routine_name"] == "morning_briefing"

    def test_catch_me_up_matches(self, routine_cmd):
        result = self._pre_route(routine_cmd, "catch me up")
        assert result is not None
        assert result.arguments["routine_name"] == "morning_briefing"

    def test_whats_happening_today_matches(self, routine_cmd):
        result = self._pre_route(routine_cmd, "what's happening today")
        assert result is not None
        assert result.arguments["routine_name"] == "morning_briefing"

    def test_good_morning_still_matches_good_morning(self, routine_cmd):
        """'good morning' should still match the good_morning routine, not morning_briefing."""
        result = self._pre_route(routine_cmd, "good morning")
        assert result is not None
        assert result.arguments["routine_name"] == "good_morning"


# ===================================================================
# Default seeding tests
# ===================================================================

class TestDefaultSeeding:
    """Verify new defaults are merged into existing DB without overwriting."""

    def test_new_defaults_added_to_existing_db(self, routine_cmd):
        """When DB has good_morning but not morning_briefing, it should be added."""
        existing_row = {
            "_data_key": "good_morning",
            "trigger_phrases": ["good morning"],
            "steps": [],
            "response_instruction": "Custom instruction.",
        }

        mock_db = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_all.return_value = [dict(existing_row)]

        with (
            patch("commands.routine_command.SessionLocal", return_value=mock_db),
            patch("commands.routine_command.CommandDataRepository", return_value=mock_repo),
        ):
            from commands.routine_command import _load_routines
            routines = _load_routines()

        # Should have the existing good_morning + all missing defaults
        assert "good_morning" in routines
        assert "morning_briefing" in routines
        # good_morning should use the custom instruction, not overwrite
        assert routines["good_morning"]["response_instruction"] == "Custom instruction."
        # morning_briefing should have been saved to repo
        save_calls = {call[0][1] for call in mock_repo.save.call_args_list}
        assert "morning_briefing" in save_calls
        # good_morning should NOT have been re-saved
        assert "good_morning" not in save_calls

    def test_fully_seeded_db_no_extra_saves(self, routine_cmd):
        """When DB has all defaults, no extra saves happen."""
        defaults = routine_cmd._default_routines()
        rows = []
        for name, defn in defaults.items():
            row = dict(defn)
            row["_data_key"] = name
            rows.append(row)

        mock_db = MagicMock()
        mock_repo = MagicMock()
        mock_repo.get_all.return_value = rows

        with (
            patch("commands.routine_command.SessionLocal", return_value=mock_db),
            patch("commands.routine_command.CommandDataRepository", return_value=mock_repo),
        ):
            from commands.routine_command import _load_routines
            routines = _load_routines()

        assert len(routines) == len(defaults)
        mock_repo.save.assert_not_called()
