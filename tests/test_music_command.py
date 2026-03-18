"""
Unit tests for MusicCommand.

Tests the unified music command for both playing content and
controlling playback via Music Assistant integration.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commands.music_command import MusicCommand
from core.ijarvis_package import JarvisPackage
from core.request_information import RequestInformation


@pytest.fixture
def music_command():
    return MusicCommand()


@pytest.fixture
def mock_request_info():
    mock = MagicMock(spec=RequestInformation)
    mock.node_id = "bedroom-pi"
    return mock


class TestMusicCommandProperties:
    def test_command_name(self, music_command):
        assert music_command.command_name == "music"

    def test_description(self, music_command):
        desc = music_command.description.lower()
        assert "play" in desc
        assert "music" in desc

    def test_keywords(self, music_command):
        keywords = music_command.keywords
        # Play keywords
        assert "play" in keywords
        assert "music" in keywords
        assert "song" in keywords
        assert "album" in keywords
        # Control keywords
        assert "pause" in keywords
        assert "volume" in keywords
        assert "shuffle" in keywords

    def test_parameters(self, music_command):
        params = music_command.parameters
        param_names = [p.name for p in params]

        assert "action" in param_names
        assert "query" in param_names
        assert "media_type" in param_names
        assert "player" in param_names
        assert "queue_option" in param_names
        assert "volume_level" in param_names

        # action is required
        action_param = next(p for p in params if p.name == "action")
        assert action_param.required is True
        assert "play" in action_param.enum_values
        assert "pause" in action_param.enum_values
        assert "next" in action_param.enum_values

        # query is NOT required globally (only for action=play)
        query_param = next(p for p in params if p.name == "query")
        assert query_param.required is False

        # media_type has enum values and is refinable
        media_type_param = next(p for p in params if p.name == "media_type")
        assert media_type_param.enum_values is not None
        assert "radio" in media_type_param.enum_values
        assert media_type_param.refinable is True

    def test_required_secrets(self, music_command):
        secrets = music_command.required_secrets
        secret_keys = [s.key for s in secrets]
        assert "MUSIC_ASSISTANT_URL" in secret_keys
        assert "MUSIC_ASSISTANT_TOKEN" in secret_keys

    def test_required_packages(self, music_command):
        packages = music_command.required_packages
        assert len(packages) >= 1
        package_names = [p.name for p in packages]
        assert "music-assistant-client" in package_names
        ma_package = next(p for p in packages if p.name == "music-assistant-client")
        assert isinstance(ma_package, JarvisPackage)


class TestMusicCommandExamples:
    def test_prompt_examples(self, music_command):
        examples = music_command.generate_prompt_examples()
        assert len(examples) >= 5

        primary = next((e for e in examples if e.is_primary), None)
        assert primary is not None
        assert primary.expected_parameters["action"] == "play"
        assert "query" in primary.expected_parameters

        # Has both play and control examples
        actions = [e.expected_parameters.get("action") for e in examples]
        assert "play" in actions
        assert "pause" in actions

    def test_adapter_examples(self, music_command):
        examples = music_command.generate_adapter_examples()
        assert len(examples) >= 20

        # Play variety
        has_artist = any(
            e.expected_parameters.get("media_type") == "artist" for e in examples
        )
        has_radio = any(
            e.expected_parameters.get("media_type") == "radio" for e in examples
        )
        assert has_artist
        assert has_radio

        # Control variety
        actions = [e.expected_parameters.get("action") for e in examples]
        assert "pause" in actions
        assert "resume" in actions
        assert "next" in actions
        assert "volume_up" in actions


class TestMusicCommandRules:
    def test_has_critical_rules(self, music_command):
        assert len(music_command.critical_rules) >= 1

    def test_no_antipatterns(self, music_command):
        # Merged command has no sibling to confuse with
        assert len(music_command.antipatterns) == 0


class TestMusicCommandRunPlay:
    """Test play (search + play) actions"""

    def test_run_missing_action(self, music_command, mock_request_info):
        response = music_command.run(mock_request_info)
        assert response.success is False
        assert "action" in response.error_details.lower()

    def test_run_play_missing_query(self, music_command, mock_request_info):
        response = music_command.run(mock_request_info, action="play")
        assert response.success is False
        assert "query" in response.error_details.lower()

    def test_run_play_success(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.search_and_play = AsyncMock(return_value={
                    "success": True,
                    "item": {"name": "Karma Police", "type": "track"},
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(mock_request_info, action="play", query="Karma Police")
        assert response.success is True
        assert response.context_data["action"] == "now_playing"

    def test_run_play_no_results(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.search_and_play = AsyncMock(return_value={
                    "success": False, "error": "No results",
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(mock_request_info, action="play", query="asdfghjkl")
        assert response.success is False
        assert "no results" in response.error_details.lower()

    def test_run_play_with_player(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.search_and_play = AsyncMock(return_value={
                    "success": True,
                    "item": {"name": "Radiohead", "type": "artist"},
                })
                mock_service.get_player_by_name = AsyncMock(return_value={
                    "id": "kitchen_speaker", "name": "Kitchen Echo",
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                response = music_command.run(
                    mock_request_info, action="play", query="Radiohead", player="Kitchen Echo",
                )
        assert response.success is True
        mock_service.get_player_by_name.assert_awaited_with("Kitchen Echo")

    def test_run_play_player_not_found(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.get_player_by_name = AsyncMock(return_value=None)
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                response = music_command.run(
                    mock_request_info, action="play", query="Radiohead", player="Nonexistent",
                )
        assert response.success is False
        assert "speaker" in response.error_details.lower()


class TestMusicCommandRunControl:
    """Test control (transport/volume/shuffle/repeat) actions"""

    def test_run_pause(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.pause = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(mock_request_info, action="pause")
        assert response.success is True
        mock_service.pause.assert_awaited_once()

    def test_run_resume(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.resume = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(mock_request_info, action="resume")
        assert response.success is True
        mock_service.resume.assert_awaited_once()

    def test_run_next(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.next_track = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(mock_request_info, action="next")
        assert response.success is True
        mock_service.next_track.assert_awaited_once()

    def test_run_volume_set(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.set_volume = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(
                        mock_request_info, action="volume_set", volume_level=50,
                    )
        assert response.success is True
        mock_service.set_volume.assert_awaited_once_with("player_123", 50)

    def test_run_volume_up(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.volume_up = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(mock_request_info, action="volume_up")
        assert response.success is True
        mock_service.volume_up.assert_awaited_once()

    def test_run_shuffle_on(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.set_shuffle = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                with patch.object(music_command, "_get_default_player", return_value="player_123"):
                    response = music_command.run(mock_request_info, action="shuffle_on")
        assert response.success is True
        mock_service.set_shuffle.assert_awaited_once_with("player_123", True)

    def test_run_invalid_action(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            response = music_command.run(mock_request_info, action="invalid_action")
        assert response.success is False
        assert "invalid" in response.error_details.lower()

    def test_run_control_with_player(self, music_command, mock_request_info):
        with patch("commands.music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"
            with patch.object(music_command, "_get_music_service") as mock_svc:
                mock_service = AsyncMock()
                mock_service.pause = AsyncMock()
                mock_service.get_player_by_name = AsyncMock(return_value={
                    "id": "kitchen_speaker", "name": "Kitchen Echo",
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_svc.return_value = mock_service
                response = music_command.run(
                    mock_request_info, action="pause", player="Kitchen Echo",
                )
        assert response.success is True
        mock_service.get_player_by_name.assert_awaited_with("Kitchen Echo")
