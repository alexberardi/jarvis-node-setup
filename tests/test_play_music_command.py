"""
Unit tests for PlayMusicCommand.

Tests the play_music command for searching and playing music
via Music Assistant integration.
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commands.play_music_command import PlayMusicCommand
from core.ijarvis_package import JarvisPackage
from core.request_information import RequestInformation


@pytest.fixture
def play_music_command():
    """Create a PlayMusicCommand instance"""
    return PlayMusicCommand()


@pytest.fixture
def mock_request_info():
    """Create a mock RequestInformation"""
    mock = MagicMock(spec=RequestInformation)
    mock.node_id = "bedroom-pi"
    return mock


class TestPlayMusicCommandProperties:
    """Test command properties"""

    def test_command_name(self, play_music_command):
        assert play_music_command.command_name == "play_music"

    def test_description(self, play_music_command):
        desc = play_music_command.description.lower()
        assert "music" in desc
        assert "play" in desc

    def test_keywords(self, play_music_command):
        keywords = play_music_command.keywords
        assert "play" in keywords
        assert "music" in keywords
        assert "song" in keywords
        assert "album" in keywords

    def test_parameters(self, play_music_command):
        params = play_music_command.parameters
        param_names = [p.name for p in params]

        assert "query" in param_names
        assert "media_type" in param_names
        assert "player" in param_names
        assert "queue_option" in param_names

        # query is required
        query_param = next(p for p in params if p.name == "query")
        assert query_param.required is True

        # media_type has enum values
        media_type_param = next(p for p in params if p.name == "media_type")
        assert media_type_param.enum_values is not None
        assert "track" in media_type_param.enum_values
        assert "album" in media_type_param.enum_values
        assert "artist" in media_type_param.enum_values

    def test_required_secrets(self, play_music_command):
        secrets = play_music_command.required_secrets
        secret_keys = [s.key for s in secrets]

        assert "MUSIC_ASSISTANT_URL" in secret_keys

        # MUSIC_ASSISTANT_URL is required
        ma_url_secret = next(s for s in secrets if s.key == "MUSIC_ASSISTANT_URL")
        assert ma_url_secret.required is True
        assert ma_url_secret.scope == "integration"


class TestPlayMusicCommandExtensions:
    """Test new IJarvisCommand extensions"""

    def test_required_packages(self, play_music_command):
        packages = play_music_command.required_packages

        assert len(packages) >= 1
        package_names = [p.name for p in packages]
        assert "music-assistant-client" in package_names

        # Check it's a proper JarvisPackage
        ma_package = next(p for p in packages if p.name == "music-assistant-client")
        assert isinstance(ma_package, JarvisPackage)
        assert ma_package.version is not None  # Should be pinned

    def test_init_data_method_exists(self, play_music_command):
        """Command has init_data method"""
        assert hasattr(play_music_command, "init_data")
        assert callable(play_music_command.init_data)


class TestPlayMusicCommandExamples:
    """Test command examples"""

    def test_prompt_examples(self, play_music_command):
        examples = play_music_command.generate_prompt_examples()
        assert len(examples) >= 5

        # Check primary example exists
        primary = next((e for e in examples if e.is_primary), None)
        assert primary is not None
        assert "query" in primary.expected_parameters

    def test_adapter_examples(self, play_music_command):
        examples = play_music_command.generate_adapter_examples()
        assert len(examples) >= 10

        # Check variety of examples
        has_artist = any(
            e.expected_parameters.get("media_type") == "artist"
            for e in examples
        )
        has_album = any(
            e.expected_parameters.get("media_type") == "album"
            for e in examples
        )
        has_track = any(
            e.expected_parameters.get("media_type") == "track"
            for e in examples
        )
        has_radio = any(
            e.expected_parameters.get("media_type") == "radio"
            for e in examples
        )
        has_player = any(
            "player" in e.expected_parameters
            for e in examples
        )

        assert has_artist
        assert has_album
        assert has_track
        assert has_radio
        assert has_player


class TestPlayMusicCommandRules:
    """Test command rules and antipatterns"""

    def test_has_critical_rules(self, play_music_command):
        critical_rules = play_music_command.critical_rules
        assert len(critical_rules) >= 1

    def test_has_antipatterns(self, play_music_command):
        antipatterns = play_music_command.antipatterns
        assert len(antipatterns) >= 1

        # Should have antipattern for control_music
        control_antipattern = next(
            (a for a in antipatterns if a.command_name == "control_music"),
            None
        )
        assert control_antipattern is not None


class TestPlayMusicCommandRun:
    """Test command execution"""

    def test_run_missing_query(self, play_music_command, mock_request_info):
        """Error when query is missing"""
        response = play_music_command.run(mock_request_info)

        assert response.success is False
        assert "query" in response.error_details.lower() or "required" in response.error_details.lower()

    def test_run_search_success(self, play_music_command, mock_request_info):
        """Successful music search and play"""
        with patch("commands.play_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(play_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.search_and_play = AsyncMock(return_value={
                    "success": True,
                    "item": {"name": "Karma Police", "type": "track"}
                })
                mock_service.get_player_by_name = AsyncMock(return_value={
                    "id": "player_123",
                    "name": "Bedroom Speaker"
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(play_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = play_music_command.run(
                        mock_request_info,
                        query="Karma Police"
                    )

        assert response.success is True
        assert response.context_data is not None
        assert "now_playing" in response.context_data.get("action", "").lower() or \
               response.context_data.get("item") is not None

    def test_run_search_no_results(self, play_music_command, mock_request_info):
        """Handle no search results gracefully"""
        with patch("commands.play_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(play_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.search_and_play = AsyncMock(return_value={
                    "success": False,
                    "error": "No results for 'asdfghjkl'"
                })
                mock_service.get_player_by_name = AsyncMock(return_value={
                    "id": "player_123",
                    "name": "Bedroom Speaker"
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(play_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = play_music_command.run(
                        mock_request_info,
                        query="asdfghjkl"
                    )

        assert response.success is False
        assert "no results" in response.error_details.lower()

    def test_run_with_explicit_player(self, play_music_command, mock_request_info):
        """Play on explicitly specified player"""
        with patch("commands.play_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(play_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.search_and_play = AsyncMock(return_value={
                    "success": True,
                    "item": {"name": "Radiohead", "type": "artist"}
                })
                mock_service.get_player_by_name = AsyncMock(return_value={
                    "id": "kitchen_speaker",
                    "name": "Kitchen Echo"
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                response = play_music_command.run(
                    mock_request_info,
                    query="Radiohead",
                    player="Kitchen Echo"
                )

        assert response.success is True
        mock_service.get_player_by_name.assert_awaited_with("Kitchen Echo")

    def test_run_player_not_found(self, play_music_command, mock_request_info):
        """Error when specified player not found"""
        with patch("commands.play_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(play_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.get_player_by_name = AsyncMock(return_value=None)
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                response = play_music_command.run(
                    mock_request_info,
                    query="Radiohead",
                    player="Nonexistent Speaker"
                )

        assert response.success is False
        assert "speaker" in response.error_details.lower() or \
               "player" in response.error_details.lower()
