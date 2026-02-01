"""
Unit tests for MusicAssistantService.

Tests the async wrapper around the official music-assistant-client.
Uses mocks since the actual client requires a running Music Assistant server.
"""

from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.music_assistant_service import MusicAssistantService


@pytest.fixture
def service():
    """Create a MusicAssistantService instance"""
    return MusicAssistantService(url="ws://localhost:8095/ws")


class TestServiceCreation:
    """Test service instantiation"""

    def test_create_with_url(self):
        """Service can be created with a URL"""
        service = MusicAssistantService(url="ws://192.168.1.50:8095/ws")
        assert service.url == "ws://192.168.1.50:8095/ws"

    def test_client_initially_none(self):
        """Client is None before connect"""
        service = MusicAssistantService(url="ws://localhost:8095/ws")
        assert service._client is None

    def test_connected_initially_false(self):
        """Not connected before connect() called"""
        service = MusicAssistantService(url="ws://localhost:8095/ws")
        assert service.connected is False


class TestConnection:
    """Test connect/disconnect behavior"""

    @pytest.mark.asyncio
    async def test_connect_creates_client(self, service):
        """connect() creates and connects the client"""
        with patch("services.music_assistant_service.MusicAssistantClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client_class.return_value = mock_client

            await service.connect()

            mock_client_class.assert_called_once_with(service.url, None)
            mock_client.connect.assert_awaited_once()
            mock_client.players.fetch_state.assert_awaited_once()
            assert service.connected is True

    @pytest.mark.asyncio
    async def test_disconnect_closes_client(self, service):
        """disconnect() closes the client connection"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.disconnect()

        mock_client.disconnect.assert_awaited_once()
        assert service.connected is False


class TestGetPlayers:
    """Test player retrieval"""

    @pytest.mark.asyncio
    async def test_get_players_returns_list(self, service):
        """get_players() returns list of player dicts"""
        mock_player = MagicMock()
        mock_player.player_id = "player_123"
        mock_player.name = "Kitchen Speaker"
        mock_player.state.value = "idle"

        mock_client = MagicMock()
        mock_client.players.players = [mock_player]
        service._client = mock_client
        service._connected = True

        players = await service.get_players()

        assert len(players) == 1
        assert players[0]["id"] == "player_123"
        assert players[0]["name"] == "Kitchen Speaker"
        assert players[0]["state"] == "idle"

    @pytest.mark.asyncio
    async def test_get_player_by_name_exact_match(self, service):
        """get_player_by_name() finds exact match"""
        mock_player = MagicMock()
        mock_player.player_id = "player_456"
        mock_player.name = "Bedroom Echo"
        mock_player.state.value = "playing"

        mock_client = MagicMock()
        mock_client.players.players = [mock_player]
        # Simulate get_by_name not existing to use fallback
        del mock_client.players.get_by_name
        service._client = mock_client
        service._connected = True

        player = await service.get_player_by_name("Bedroom Echo")

        assert player is not None
        assert player["id"] == "player_456"

    @pytest.mark.asyncio
    async def test_get_player_by_name_fuzzy_match(self, service):
        """get_player_by_name() does case-insensitive partial match"""
        mock_player = MagicMock()
        mock_player.player_id = "player_789"
        mock_player.name = "Living Room Sonos"
        mock_player.state.value = "idle"

        mock_client = MagicMock()
        mock_client.players.players = [mock_player]
        # Simulate get_by_name not existing to use fallback
        del mock_client.players.get_by_name
        service._client = mock_client
        service._connected = True

        player = await service.get_player_by_name("living room")

        assert player is not None
        assert player["id"] == "player_789"

    @pytest.mark.asyncio
    async def test_get_player_by_name_not_found(self, service):
        """get_player_by_name() returns None if not found"""
        mock_player = MagicMock()
        mock_player.player_id = "player_123"
        mock_player.name = "Kitchen Speaker"
        mock_player.state.value = "idle"

        mock_client = MagicMock()
        mock_client.players.players = [mock_player]
        # Simulate get_by_name not existing to use fallback
        del mock_client.players.get_by_name
        service._client = mock_client
        service._connected = True

        player = await service.get_player_by_name("Bedroom")

        assert player is None


class TestPlaybackControls:
    """Test playback control methods"""

    @pytest.mark.asyncio
    async def test_pause(self, service):
        """pause() calls player_queues.pause"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.pause(queue_id="queue_123")

        mock_client.player_queues.pause.assert_awaited_once_with("queue_123")

    @pytest.mark.asyncio
    async def test_resume(self, service):
        """resume() calls player_queues.resume"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.resume(queue_id="queue_123")

        mock_client.player_queues.resume.assert_awaited_once_with("queue_123")

    @pytest.mark.asyncio
    async def test_stop(self, service):
        """stop() calls player_queues.stop"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.stop(queue_id="queue_123")

        mock_client.player_queues.stop.assert_awaited_once_with("queue_123")

    @pytest.mark.asyncio
    async def test_next_track(self, service):
        """next_track() calls player_queues.next"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.next_track(queue_id="queue_123")

        mock_client.player_queues.next.assert_awaited_once_with("queue_123")

    @pytest.mark.asyncio
    async def test_previous_track(self, service):
        """previous_track() calls player_queues.previous"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.previous_track(queue_id="queue_123")

        mock_client.player_queues.previous.assert_awaited_once_with("queue_123")


class TestVolumeControls:
    """Test volume control methods"""

    @pytest.mark.asyncio
    async def test_set_volume(self, service):
        """set_volume() calls players.volume_set"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.set_volume(player_id="player_123", level=75)

        mock_client.players.volume_set.assert_awaited_once_with("player_123", 75)

    @pytest.mark.asyncio
    async def test_volume_up(self, service):
        """volume_up() calls players.volume_up"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.volume_up(player_id="player_123")

        mock_client.players.volume_up.assert_awaited_once_with("player_123")

    @pytest.mark.asyncio
    async def test_volume_down(self, service):
        """volume_down() calls players.volume_down"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.volume_down(player_id="player_123")

        mock_client.players.volume_down.assert_awaited_once_with("player_123")


class TestShuffleRepeat:
    """Test shuffle and repeat controls"""

    @pytest.mark.asyncio
    async def test_set_shuffle_on(self, service):
        """set_shuffle() enables shuffle"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.set_shuffle(queue_id="queue_123", enabled=True)

        mock_client.player_queues.shuffle.assert_awaited_once_with("queue_123", True)

    @pytest.mark.asyncio
    async def test_set_shuffle_off(self, service):
        """set_shuffle() disables shuffle"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        await service.set_shuffle(queue_id="queue_123", enabled=False)

        mock_client.player_queues.shuffle.assert_awaited_once_with("queue_123", False)

    @pytest.mark.asyncio
    async def test_set_repeat(self, service):
        """set_repeat() sets repeat mode"""
        mock_client = AsyncMock()
        service._client = mock_client
        service._connected = True

        # RepeatMode is an enum from music_assistant_models
        from services.music_assistant_service import RepeatMode

        await service.set_repeat(queue_id="queue_123", mode=RepeatMode.ONE)

        mock_client.player_queues.repeat.assert_awaited_once_with("queue_123", RepeatMode.ONE)


class TestSearchAndPlay:
    """Test search and play functionality"""

    @pytest.mark.asyncio
    async def test_search_and_play_success(self, service):
        """search_and_play() searches and plays content"""
        from services.music_assistant_service import MediaType, QueueOption

        # Mock search result
        mock_track = MagicMock()
        mock_track.name = "Karma Police"
        mock_track.media_type.value = "track"

        mock_search_result = MagicMock()
        mock_search_result.tracks = [mock_track]
        mock_search_result.artists = []
        mock_search_result.albums = []
        mock_search_result.playlists = []
        mock_search_result.radio = []

        mock_client = AsyncMock()
        mock_client.music.search = AsyncMock(return_value=mock_search_result)
        mock_client.player_queues.play_media = AsyncMock()
        service._client = mock_client
        service._connected = True

        result = await service.search_and_play(
            query="Karma Police",
            queue_id="queue_123",
            media_type=MediaType.TRACK
        )

        assert result["success"] is True
        assert result["item"]["name"] == "Karma Police"
        mock_client.player_queues.play_media.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_search_and_play_no_results(self, service):
        """search_and_play() returns error when no results"""
        from services.music_assistant_service import MediaType

        mock_search_result = MagicMock()
        mock_search_result.tracks = []
        mock_search_result.artists = []
        mock_search_result.albums = []
        mock_search_result.playlists = []
        mock_search_result.radio = []

        mock_client = AsyncMock()
        mock_client.music.search = AsyncMock(return_value=mock_search_result)
        service._client = mock_client
        service._connected = True

        result = await service.search_and_play(
            query="Nonexistent Song",
            queue_id="queue_123",
            media_type=MediaType.TRACK
        )

        assert result["success"] is False
        assert "no results" in result["error"].lower()
