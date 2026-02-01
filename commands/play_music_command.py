#!/usr/bin/env python3
"""
Play Music command for Jarvis.

Searches for and plays music content via Music Assistant integration.
Supports artists, albums, tracks, playlists, and radio/genre requests.
"""

import asyncio
from typing import Any, Dict, List, Optional

from core.command_response import CommandResponse
from core.ijarvis_command import CommandAntipattern, CommandExample, IJarvisCommand
from core.ijarvis_package import JarvisPackage
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation
from services.music_assistant_service import (
    MediaType,
    MusicAssistantService,
    QueueOption,
)
from services.secret_service import get_secret_value


class PlayMusicCommand(IJarvisCommand):
    """Command for playing music via Music Assistant"""

    @property
    def command_name(self) -> str:
        return "play_music"

    @property
    def description(self) -> str:
        return (
            "Play music - search for artists, albums, songs, playlists, "
            "or radio stations via Music Assistant"
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "play", "music", "song", "album", "artist", "playlist",
            "listen", "put on", "throw on", "queue", "radio"
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "query",
                "string",
                required=True,
                description="What to play: artist name, album, song, playlist, or genre"
            ),
            JarvisParameter(
                "media_type",
                "string",
                required=False,
                enum_values=["track", "album", "artist", "playlist", "radio"],
                description="Type of content. Infer from context if not specified."
            ),
            JarvisParameter(
                "player",
                "string",
                required=False,
                description="Target speaker name. If not specified, use room's default speaker."
            ),
            JarvisParameter(
                "queue_option",
                "string",
                required=False,
                enum_values=["play", "next", "add"],
                description="'play' replaces queue (default), 'next' plays after current, 'add' appends to queue"
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "MUSIC_ASSISTANT_URL",
                "Music Assistant WebSocket URL (e.g., ws://192.168.1.50:8095/ws)",
                "integration",
                "string"
            ),
        ]

    @property
    def required_packages(self) -> List[JarvisPackage]:
        return [
            JarvisPackage("music-assistant-client", ">=1.3.0"),
        ]

    @property
    def rules(self) -> List[str]:
        return [
            "For genre requests like 'play some jazz', use media_type='radio'",
            "For 'play [artist name]', use media_type='artist' to play their catalog",
            "If user says 'queue' or 'add to queue', use queue_option='add'",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "If user specifies a speaker/player, use the 'player' parameter",
            "If no player specified, the room's default speaker will be used",
            "For 'play some jazz' or genre requests, use media_type='radio'",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                "control_music",
                "Playback controls: pause, stop, skip, volume, shuffle. No content search."
            )
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration"""
        return [
            CommandExample(
                voice_command="Play Radiohead",
                expected_parameters={"query": "Radiohead", "media_type": "artist"},
                is_primary=True
            ),
            CommandExample(
                voice_command="Play OK Computer",
                expected_parameters={"query": "OK Computer", "media_type": "album"}
            ),
            CommandExample(
                voice_command="Play Karma Police",
                expected_parameters={"query": "Karma Police", "media_type": "track"}
            ),
            CommandExample(
                voice_command="Play some jazz",
                expected_parameters={"query": "jazz", "media_type": "radio"}
            ),
            CommandExample(
                voice_command="Play Taylor Swift in the kitchen",
                expected_parameters={
                    "query": "Taylor Swift",
                    "media_type": "artist",
                    "player": "Kitchen Echo"
                }
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training"""
        items = [
            # Artist plays
            ("Play Radiohead", {"query": "Radiohead", "media_type": "artist"}),
            ("Put on some Beatles", {"query": "Beatles", "media_type": "artist"}),
            ("Listen to Taylor Swift", {"query": "Taylor Swift", "media_type": "artist"}),
            ("Play some Daft Punk", {"query": "Daft Punk", "media_type": "artist"}),

            # Album plays
            ("Play OK Computer", {"query": "OK Computer", "media_type": "album"}),
            ("Play the album Abbey Road", {"query": "Abbey Road", "media_type": "album"}),
            ("Put on Dark Side of the Moon", {"query": "Dark Side of the Moon", "media_type": "album"}),

            # Track plays
            ("Play Karma Police", {"query": "Karma Police", "media_type": "track"}),
            ("Play the song Bohemian Rhapsody", {"query": "Bohemian Rhapsody", "media_type": "track"}),
            ("Put on Hey Jude", {"query": "Hey Jude", "media_type": "track"}),

            # Radio/genre plays
            ("Play some jazz", {"query": "jazz", "media_type": "radio"}),
            ("Put on classical music", {"query": "classical", "media_type": "radio"}),
            ("Play relaxing music", {"query": "relaxing", "media_type": "radio"}),
            ("Play some 80s hits", {"query": "80s hits", "media_type": "radio"}),

            # With player specified
            ("Play Taylor Swift in the kitchen", {
                "query": "Taylor Swift",
                "media_type": "artist",
                "player": "Kitchen Echo"
            }),
            ("Play jazz in the living room", {
                "query": "jazz",
                "media_type": "radio",
                "player": "Living Room Sonos"
            }),
            ("Play Beatles on the bedroom speaker", {
                "query": "Beatles",
                "media_type": "artist",
                "player": "Bedroom Speaker"
            }),

            # Queue options
            ("Queue up Bohemian Rhapsody", {
                "query": "Bohemian Rhapsody",
                "media_type": "track",
                "queue_option": "add"
            }),
            ("Add this song to the queue: Yellow", {
                "query": "Yellow",
                "media_type": "track",
                "queue_option": "add"
            }),
            ("Play Stairway to Heaven next", {
                "query": "Stairway to Heaven",
                "media_type": "track",
                "queue_option": "next"
            }),
        ]

        examples = []
        for i, (utterance, params) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters=params,
                is_primary=(i == 0)
            ))
        return examples

    def init_data(self) -> Dict[str, Any]:
        """
        Sync Music Assistant players to Command Center devices table.

        Called manually on first install:
            python scripts/init_data.py --command play_music
        """
        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        if not ma_url:
            return {"status": "error", "message": "MUSIC_ASSISTANT_URL not configured"}

        async def sync():
            service = MusicAssistantService(ma_url)
            try:
                await service.connect()
                players = await service.get_players()

                # TODO: Sync players to Command Center devices table
                # cc = CommandCenterClient()
                # for player in players:
                #     await cc.upsert_device({
                #         "name": player["name"],
                #         "type": "speaker",
                #         "metadata": {
                #             "source": "music_assistant",
                #             "ma_player_id": player["id"],
                #         }
                #     })

                await service.disconnect()
                return len(players)
            except Exception as e:
                await service.disconnect()
                raise e

        try:
            count = asyncio.run(sync())
            return {
                "status": "success",
                "devices_synced": count,
                "message": f"Found {count} Music Assistant players"
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the play music command"""
        query = kwargs.get("query")
        media_type_str = kwargs.get("media_type")
        player_name = kwargs.get("player")
        queue_option_str = kwargs.get("queue_option", "play")

        # Validate query
        if not query:
            return CommandResponse.error_response(
                error_details="Query is required - what would you like to play?",
                context_data={"error": "missing_query"}
            )

        # Get Music Assistant URL
        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        if not ma_url:
            return CommandResponse.error_response(
                error_details="Music Assistant is not configured",
                context_data={"error": "not_configured"}
            )

        # Map string to enum
        media_type = self._parse_media_type(media_type_str)
        queue_option = self._parse_queue_option(queue_option_str)

        # Run async operation
        async def play():
            service = self._get_music_service()
            try:
                await service.connect()

                # Resolve player
                player_id = await self._resolve_player(
                    service, player_name, request_info.node_id
                )
                if player_id is None and player_name:
                    await service.disconnect()
                    return CommandResponse.error_response(
                        error_details=f"I don't see a speaker called '{player_name}'",
                        context_data={"error": "player_not_found", "player": player_name}
                    )

                if player_id is None:
                    player_id = self._get_default_player(request_info.node_id)
                    if player_id is None:
                        await service.disconnect()
                        return CommandResponse.error_response(
                            error_details="No default speaker configured",
                            context_data={"error": "no_default_player"}
                        )

                # Search and play
                result = await service.search_and_play(
                    query=query,
                    queue_id=player_id,
                    media_type=media_type,
                    queue_option=queue_option
                )

                await service.disconnect()

                if result["success"]:
                    return CommandResponse.success_response(
                        context_data={
                            "action": "now_playing",
                            "item": result["item"],
                            "query": query,
                            "player_id": player_id,
                            "message": f"Now playing {result['item']['name']}"
                        },
                        wait_for_input=False
                    )
                else:
                    return CommandResponse.error_response(
                        error_details=f"No results found for '{query}'",
                        context_data={
                            "error": "no_results",
                            "query": query
                        }
                    )

            except Exception as e:
                await service.disconnect()
                return CommandResponse.error_response(
                    error_details=f"Music playback error: {str(e)}",
                    context_data={"error": str(e)}
                )

        return asyncio.run(play())

    def _get_music_service(self) -> MusicAssistantService:
        """Get a MusicAssistantService instance"""
        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        return MusicAssistantService(ma_url)

    async def _resolve_player(
        self,
        service: MusicAssistantService,
        player_name: Optional[str],
        node_id: str
    ) -> Optional[str]:
        """
        Resolve player name to player ID.

        Args:
            service: Music Assistant service
            player_name: Optional player name from user
            node_id: Node ID for room context

        Returns:
            Player ID if found, None otherwise
        """
        if player_name:
            player = await service.get_player_by_name(player_name)
            return player["id"] if player else None
        return None

    def _get_default_player(self, node_id: str) -> Optional[str]:
        """
        Get the default player for the node's room.

        Args:
            node_id: Node ID to look up room context

        Returns:
            Default player ID, or None if not configured
        """
        # TODO: Query Command Center for room's default speaker
        # cc = CommandCenterClient()
        # device = cc.get_default_device(node_id, "speaker")
        # return device["metadata"]["ma_player_id"] if device else None

        # For now, return None - user must specify player
        return None

    def _parse_media_type(self, media_type_str: Optional[str]) -> Optional[MediaType]:
        """Parse media type string to enum"""
        if not media_type_str:
            return None
        mapping = {
            "track": MediaType.TRACK,
            "album": MediaType.ALBUM,
            "artist": MediaType.ARTIST,
            "playlist": MediaType.PLAYLIST,
            "radio": MediaType.RADIO,
        }
        return mapping.get(media_type_str.lower())

    def _parse_queue_option(self, queue_option_str: Optional[str]) -> QueueOption:
        """Parse queue option string to enum"""
        mapping = {
            "play": QueueOption.PLAY,
            "next": QueueOption.NEXT,
            "add": QueueOption.ADD,
        }
        return mapping.get(queue_option_str.lower() if queue_option_str else "play", QueueOption.PLAY)
