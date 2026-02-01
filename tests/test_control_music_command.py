"""
Unit tests for ControlMusicCommand.

Tests the control_music command for playback controls:
pause, resume, skip, volume, shuffle, repeat.
"""

from typing import Any, Dict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from commands.control_music_command import ControlMusicCommand
from core.request_information import RequestInformation


@pytest.fixture
def control_music_command():
    """Create a ControlMusicCommand instance"""
    return ControlMusicCommand()


@pytest.fixture
def mock_request_info():
    """Create a mock RequestInformation"""
    mock = MagicMock(spec=RequestInformation)
    mock.node_id = "bedroom-pi"
    return mock


class TestControlMusicCommandProperties:
    """Test command properties"""

    def test_command_name(self, control_music_command):
        assert control_music_command.command_name == "control_music"

    def test_description(self, control_music_command):
        desc = control_music_command.description.lower()
        assert "music" in desc
        assert "control" in desc or "playback" in desc

    def test_keywords(self, control_music_command):
        keywords = control_music_command.keywords
        assert "pause" in keywords
        assert "resume" in keywords
        assert "skip" in keywords or "next" in keywords
        assert "volume" in keywords

    def test_parameters(self, control_music_command):
        params = control_music_command.parameters
        param_names = [p.name for p in params]

        assert "action" in param_names
        assert "volume_level" in param_names
        assert "player" in param_names

        # action is required
        action_param = next(p for p in params if p.name == "action")
        assert action_param.required is True

        # action has enum values
        assert action_param.enum_values is not None
        assert "pause" in action_param.enum_values
        assert "resume" in action_param.enum_values
        assert "next" in action_param.enum_values
        assert "volume_set" in action_param.enum_values

    def test_required_secrets(self, control_music_command):
        secrets = control_music_command.required_secrets
        secret_keys = [s.key for s in secrets]

        assert "MUSIC_ASSISTANT_URL" in secret_keys


class TestControlMusicCommandExamples:
    """Test command examples"""

    def test_prompt_examples(self, control_music_command):
        examples = control_music_command.generate_prompt_examples()
        assert len(examples) >= 5

        # Check primary example exists
        primary = next((e for e in examples if e.is_primary), None)
        assert primary is not None
        assert "action" in primary.expected_parameters

    def test_adapter_examples(self, control_music_command):
        examples = control_music_command.generate_adapter_examples()
        assert len(examples) >= 10

        # Check variety of actions
        actions = [e.expected_parameters.get("action") for e in examples]
        assert "pause" in actions
        assert "resume" in actions
        assert "next" in actions
        assert "volume_up" in actions or "volume_down" in actions


class TestControlMusicCommandRules:
    """Test command rules and antipatterns"""

    def test_has_critical_rules(self, control_music_command):
        critical_rules = control_music_command.critical_rules
        assert len(critical_rules) >= 1

    def test_has_antipatterns(self, control_music_command):
        antipatterns = control_music_command.antipatterns
        assert len(antipatterns) >= 1

        # Should have antipattern for play_music
        play_antipattern = next(
            (a for a in antipatterns if a.command_name == "play_music"),
            None
        )
        assert play_antipattern is not None


class TestControlMusicCommandRun:
    """Test command execution"""

    def test_run_missing_action(self, control_music_command, mock_request_info):
        """Error when action is missing"""
        response = control_music_command.run(mock_request_info)

        assert response.success is False
        assert "action" in response.error_details.lower() or "required" in response.error_details.lower()

    def test_run_pause(self, control_music_command, mock_request_info):
        """Pause action works"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(control_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.pause = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(control_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = control_music_command.run(
                        mock_request_info,
                        action="pause"
                    )

        assert response.success is True
        mock_service.pause.assert_awaited_once()

    def test_run_resume(self, control_music_command, mock_request_info):
        """Resume action works"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(control_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.resume = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(control_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = control_music_command.run(
                        mock_request_info,
                        action="resume"
                    )

        assert response.success is True
        mock_service.resume.assert_awaited_once()

    def test_run_next_track(self, control_music_command, mock_request_info):
        """Next track action works"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(control_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.next_track = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(control_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = control_music_command.run(
                        mock_request_info,
                        action="next"
                    )

        assert response.success is True
        mock_service.next_track.assert_awaited_once()

    def test_run_volume_set(self, control_music_command, mock_request_info):
        """Set volume action works"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(control_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.set_volume = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(control_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = control_music_command.run(
                        mock_request_info,
                        action="volume_set",
                        volume_level=50
                    )

        assert response.success is True
        mock_service.set_volume.assert_awaited_once_with("player_123", 50)

    def test_run_volume_up(self, control_music_command, mock_request_info):
        """Volume up action works"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(control_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.volume_up = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(control_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = control_music_command.run(
                        mock_request_info,
                        action="volume_up"
                    )

        assert response.success is True
        mock_service.volume_up.assert_awaited_once()

    def test_run_shuffle_on(self, control_music_command, mock_request_info):
        """Shuffle on action works"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(control_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.set_shuffle = AsyncMock()
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                with patch.object(control_music_command, "_get_default_player") as mock_default:
                    mock_default.return_value = "player_123"

                    response = control_music_command.run(
                        mock_request_info,
                        action="shuffle_on"
                    )

        assert response.success is True
        mock_service.set_shuffle.assert_awaited_once_with("player_123", True)

    def test_run_with_explicit_player(self, control_music_command, mock_request_info):
        """Control action on explicit player"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            with patch.object(control_music_command, "_get_music_service") as mock_get_service:
                mock_service = AsyncMock()
                mock_service.pause = AsyncMock()
                mock_service.get_player_by_name = AsyncMock(return_value={
                    "id": "kitchen_speaker",
                    "name": "Kitchen Echo"
                })
                mock_service.connect = AsyncMock()
                mock_service.disconnect = AsyncMock()
                mock_get_service.return_value = mock_service

                response = control_music_command.run(
                    mock_request_info,
                    action="pause",
                    player="Kitchen Echo"
                )

        assert response.success is True
        mock_service.get_player_by_name.assert_awaited_with("Kitchen Echo")

    def test_run_invalid_action(self, control_music_command, mock_request_info):
        """Error for invalid action"""
        with patch("commands.control_music_command.get_secret_value") as mock_secret:
            mock_secret.return_value = "ws://localhost:8095/ws"

            response = control_music_command.run(
                mock_request_info,
                action="invalid_action"
            )

        assert response.success is False
        assert "action" in response.error_details.lower() or "invalid" in response.error_details.lower()
