"""
Music Assistant service wrapper.

Async wrapper around the official music-assistant-client package.
Provides methods for playing music, controlling playback, and managing players.
"""

from enum import Enum
from typing import Any, Dict, List, Optional


# Re-export enums from music_assistant_models when available
# These stubs allow the code to work even without the package installed
try:
    from music_assistant_models.enums import MediaType, QueueOption, RepeatMode
except ImportError:
    # Stub enums for when music-assistant-client is not installed

    class MediaType(str, Enum):
        """Type of media content."""
        ARTIST = "artist"
        ALBUM = "album"
        TRACK = "track"
        PLAYLIST = "playlist"
        RADIO = "radio"

    class QueueOption(str, Enum):
        """Queue insertion options."""
        PLAY = "play"  # Replace queue and play
        NEXT = "next"  # Insert after current track
        ADD = "add"    # Add to end of queue

    class RepeatMode(str, Enum):
        """Repeat mode options."""
        OFF = "off"
        ONE = "one"    # Repeat current track
        ALL = "all"    # Repeat entire queue


# Import the client when available
try:
    from music_assistant_client import MusicAssistantClient
except ImportError:
    MusicAssistantClient = None  # type: ignore


class MusicAssistantService:
    """
    Wrapper around official Music Assistant client.

    Provides async methods for:
    - Connecting to Music Assistant server
    - Searching and playing music
    - Controlling playback (pause, resume, skip)
    - Managing volume and shuffle/repeat
    - Getting available players

    Usage:
        service = MusicAssistantService("ws://192.168.1.50:8095/ws")
        await service.connect()
        players = await service.get_players()
        await service.search_and_play("Radiohead", players[0]["id"])
        await service.disconnect()
    """

    def __init__(self, url: str):
        """
        Initialize the service.

        Args:
            url: Music Assistant WebSocket URL (e.g., "ws://192.168.1.50:8095/ws")
        """
        self.url = url
        self._client: Optional[Any] = None
        self._connected = False

    @property
    def connected(self) -> bool:
        """Whether the client is connected."""
        return self._connected

    async def connect(self) -> None:
        """
        Establish connection to Music Assistant.

        Raises:
            ConnectionError: If connection fails
        """
        if MusicAssistantClient is None:
            raise ImportError(
                "music-assistant-client is not installed. "
                "Install it with: pip install music-assistant-client"
            )

        self._client = MusicAssistantClient(self.url, None)
        await self._client.connect()
        # Fetch initial state to populate players list
        await self._client.players.fetch_state()
        self._connected = True

    async def disconnect(self) -> None:
        """Close connection to Music Assistant."""
        if self._client:
            await self._client.disconnect()
        self._connected = False

    # --- Players ---

    async def get_players(self) -> List[Dict[str, Any]]:
        """
        Get all available players.

        Returns:
            List of player dicts with id, name, and state
        """
        if not self._client:
            return []

        # In newer versions, players are accessed via client.players.players property
        players = self._client.players.players
        return [
            {
                "id": p.player_id,
                "name": p.name,
                "state": p.state.value if hasattr(p, 'state') and p.state else "unknown"
            }
            for p in players
        ]

    async def get_player_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Find player by name (case-insensitive partial match).

        Args:
            name: Player name to search for

        Returns:
            Player dict if found, None otherwise
        """
        # Try built-in get_by_name first if available
        if self._client and hasattr(self._client.players, 'get_by_name'):
            try:
                player = await self._client.players.get_by_name(name)
                if player:
                    return {
                        "id": player.player_id,
                        "name": player.name,
                        "state": player.state.value if hasattr(player, 'state') and player.state else "unknown"
                    }
            except Exception:
                pass  # Fall back to manual search

        # Manual fuzzy search
        players = await self.get_players()
        name_lower = name.lower()
        for p in players:
            if name_lower in p["name"].lower():
                return p
        return None

    # --- Playback Controls ---

    async def pause(self, queue_id: str) -> None:
        """Pause playback on a queue."""
        if self._client:
            await self._client.player_queues.pause(queue_id)

    async def resume(self, queue_id: str) -> None:
        """Resume playback on a queue."""
        if self._client:
            await self._client.player_queues.resume(queue_id)

    async def stop(self, queue_id: str) -> None:
        """Stop playback on a queue."""
        if self._client:
            await self._client.player_queues.stop(queue_id)

    async def next_track(self, queue_id: str) -> None:
        """Skip to next track in queue."""
        if self._client:
            await self._client.player_queues.next(queue_id)

    async def previous_track(self, queue_id: str) -> None:
        """Go to previous track in queue."""
        if self._client:
            await self._client.player_queues.previous(queue_id)

    # --- Volume Controls ---

    async def set_volume(self, player_id: str, level: int) -> None:
        """
        Set volume level.

        Args:
            player_id: Player to control
            level: Volume level 0-100
        """
        if self._client:
            await self._client.players.volume_set(player_id, level)

    async def volume_up(self, player_id: str) -> None:
        """Increase volume."""
        if self._client:
            await self._client.players.volume_up(player_id)

    async def volume_down(self, player_id: str) -> None:
        """Decrease volume."""
        if self._client:
            await self._client.players.volume_down(player_id)

    # --- Shuffle and Repeat ---

    async def set_shuffle(self, queue_id: str, enabled: bool) -> None:
        """Enable or disable shuffle."""
        if self._client:
            await self._client.player_queues.shuffle(queue_id, enabled)

    async def set_repeat(self, queue_id: str, mode: RepeatMode) -> None:
        """
        Set repeat mode.

        Args:
            queue_id: Queue to control
            mode: RepeatMode.OFF, RepeatMode.ONE, or RepeatMode.ALL
        """
        if self._client:
            await self._client.player_queues.repeat(queue_id, mode)

    # --- Search and Play ---

    async def search_and_play(
        self,
        query: str,
        queue_id: str,
        media_type: Optional[MediaType] = None,
        queue_option: QueueOption = QueueOption.PLAY,
        radio_mode: bool = False
    ) -> Dict[str, Any]:
        """
        Search for content and play it.

        Args:
            query: Search query (artist, album, track name, etc.)
            queue_id: Queue/player to play on
            media_type: Optional type filter (ARTIST, ALBUM, TRACK, etc.)
            queue_option: How to add to queue (PLAY, NEXT, ADD)
            radio_mode: Enable radio mode for continuous similar music

        Returns:
            Dict with success status and played item info
        """
        if not self._client:
            return {"success": False, "error": "Not connected"}

        # Build media types to search
        if media_type:
            media_types = [media_type]
        else:
            media_types = list(MediaType)

        # Search
        results = await self._client.music.search(query, media_types, limit=10)

        # Find best match
        item = self._pick_best_result(results, media_type)
        if not item:
            return {"success": False, "error": f"No results for '{query}'"}

        # Play
        await self._client.player_queues.play_media(
            queue_id=queue_id,
            media=item,
            option=queue_option,
            radio_mode=radio_mode
        )

        return {
            "success": True,
            "item": {"name": item.name, "type": item.media_type.value}
        }

    def _pick_best_result(
        self,
        results: Any,
        preferred_type: Optional[MediaType]
    ) -> Optional[Any]:
        """
        Pick best search result, preferring the specified type.

        Args:
            results: Search results object
            preferred_type: Preferred media type

        Returns:
            Best matching item, or None
        """
        # Check for preferred type first
        if preferred_type == MediaType.ARTIST and results.artists:
            return results.artists[0]
        if preferred_type == MediaType.ALBUM and results.albums:
            return results.albums[0]
        if preferred_type == MediaType.TRACK and results.tracks:
            return results.tracks[0]
        if preferred_type == MediaType.PLAYLIST and results.playlists:
            return results.playlists[0]
        if preferred_type == MediaType.RADIO and results.radio:
            return results.radio[0]

        # No preference - return first available
        for items in [results.tracks, results.artists, results.albums,
                      results.playlists, results.radio]:
            if items:
                return items[0]
        return None
