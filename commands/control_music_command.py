#!/usr/bin/env python3
"""
Control Music command for Jarvis.

Controls music playback: pause, resume, skip, volume, shuffle, repeat.
Does not search for content - use play_music for that.
"""

import asyncio
from typing import List, Optional

from core.command_response import CommandResponse
from core.ijarvis_command import CommandAntipattern, CommandExample, IJarvisCommand
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation
from services.music_assistant_service import MusicAssistantService, RepeatMode
from services.secret_service import get_secret_value


# Valid actions for the control_music command
VALID_ACTIONS = {
    "pause", "resume", "stop", "next", "previous",
    "shuffle_on", "shuffle_off",
    "repeat_off", "repeat_one", "repeat_all",
    "volume_up", "volume_down", "volume_set", "mute", "unmute"
}


class ControlMusicCommand(IJarvisCommand):
    """Command for controlling music playback"""

    @property
    def command_name(self) -> str:
        return "control_music"

    @property
    def description(self) -> str:
        return (
            "Control music playback: pause, resume, skip, volume, shuffle, repeat. "
            "Does not search for content - use play_music for playing specific songs/artists."
        )

    @property
    def keywords(self) -> List[str]:
        return [
            "pause", "stop", "resume", "skip", "next", "previous",
            "volume", "louder", "quieter", "mute", "shuffle", "repeat"
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "action",
                "string",
                required=True,
                enum_values=[
                    "pause", "resume", "stop", "next", "previous",
                    "shuffle_on", "shuffle_off",
                    "repeat_off", "repeat_one", "repeat_all",
                    "volume_up", "volume_down", "volume_set", "mute", "unmute"
                ],
                description="The playback action to perform"
            ),
            JarvisParameter(
                "volume_level",
                "int",
                required=False,
                description="Volume level 0-100. Only used with volume_set action."
            ),
            JarvisParameter(
                "player",
                "string",
                required=False,
                description="Target speaker. If not specified, controls current/default player."
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "MUSIC_ASSISTANT_URL",
                "Music Assistant WebSocket URL",
                "integration",
                "string"
            ),
            JarvisSecret(
                "MUSIC_ASSISTANT_TOKEN",
                "Music Assistant auth token",
                "integration",
                "string"
            ),
        ]

    @property
    def rules(self) -> List[str]:
        return [
            "Use 'resume' not 'play' when continuing paused music",
            "For 'turn up the volume' use 'volume_up'",
            "For 'set volume to 50' use 'volume_set' with volume_level=50",
            "For 'louder'/'quieter' use volume_up/volume_down",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use 'resume' not 'play' when continuing paused music",
            "This command is for playback control only, not for playing specific content",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                "play_music",
                "Playing specific content: artists, songs, albums, playlists, genres"
            )
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for prompt/tool registration"""
        return [
            CommandExample(
                voice_command="Pause the music",
                expected_parameters={"action": "pause"},
                is_primary=True
            ),
            CommandExample(
                voice_command="Resume",
                expected_parameters={"action": "resume"}
            ),
            CommandExample(
                voice_command="Skip this song",
                expected_parameters={"action": "next"}
            ),
            CommandExample(
                voice_command="Turn up the volume",
                expected_parameters={"action": "volume_up"}
            ),
            CommandExample(
                voice_command="Set volume to 50",
                expected_parameters={"action": "volume_set", "volume_level": 50}
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training"""
        items = [
            # Pause variations
            ("Pause", {"action": "pause"}),
            ("Pause the music", {"action": "pause"}),
            ("Stop playing", {"action": "pause"}),

            # Resume variations
            ("Resume", {"action": "resume"}),
            ("Continue", {"action": "resume"}),
            ("Play", {"action": "resume"}),
            ("Unpause", {"action": "resume"}),

            # Skip/next variations
            ("Skip", {"action": "next"}),
            ("Skip this song", {"action": "next"}),
            ("Next song", {"action": "next"}),
            ("Next track", {"action": "next"}),

            # Previous
            ("Go back", {"action": "previous"}),
            ("Previous song", {"action": "previous"}),
            ("Play the last song", {"action": "previous"}),

            # Volume variations
            ("Turn it up", {"action": "volume_up"}),
            ("Louder", {"action": "volume_up"}),
            ("Turn it down", {"action": "volume_down"}),
            ("Quieter", {"action": "volume_down"}),
            ("Set volume to 30", {"action": "volume_set", "volume_level": 30}),
            ("Volume 50", {"action": "volume_set", "volume_level": 50}),
            ("Mute", {"action": "mute"}),
            ("Unmute", {"action": "unmute"}),

            # Shuffle variations
            ("Shuffle on", {"action": "shuffle_on"}),
            ("Turn on shuffle", {"action": "shuffle_on"}),
            ("Shuffle off", {"action": "shuffle_off"}),
            ("Turn off shuffle", {"action": "shuffle_off"}),

            # Repeat variations
            ("Repeat this song", {"action": "repeat_one"}),
            ("Repeat all", {"action": "repeat_all"}),
            ("Stop repeating", {"action": "repeat_off"}),

            # With player specified
            ("Pause the kitchen speaker", {"action": "pause", "player": "Kitchen Speaker"}),
            ("Turn up the volume in the bedroom", {"action": "volume_up", "player": "Bedroom Speaker"}),
        ]

        examples = []
        for i, (utterance, params) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters=params,
                is_primary=(i == 0)
            ))
        return examples

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the control music command"""
        action = kwargs.get("action")
        volume_level = kwargs.get("volume_level")
        player_name = kwargs.get("player")

        # Validate action
        if not action:
            return CommandResponse.error_response(
                error_details="Action is required - what would you like to do?",
                context_data={"error": "missing_action"}
            )

        if action not in VALID_ACTIONS:
            return CommandResponse.error_response(
                error_details=f"Invalid action '{action}'. Valid actions: pause, resume, next, previous, volume_up, volume_down, volume_set, shuffle_on, shuffle_off, repeat_off, repeat_one, repeat_all, mute, unmute",
                context_data={"error": "invalid_action", "action": action}
            )

        # Get Music Assistant URL
        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        if not ma_url:
            return CommandResponse.error_response(
                error_details="Music Assistant is not configured",
                context_data={"error": "not_configured"}
            )

        # Run async operation
        async def control():
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

                # Execute action
                await self._execute_action(service, player_id, action, volume_level)

                await service.disconnect()

                return CommandResponse.success_response(
                    context_data={
                        "action": action,
                        "player_id": player_id,
                        "volume_level": volume_level,
                        "message": self._build_confirmation_message(action, volume_level)
                    },
                    wait_for_input=False
                )

            except Exception as e:
                await service.disconnect()
                return CommandResponse.error_response(
                    error_details=f"Music control error: {str(e)}",
                    context_data={"error": str(e)}
                )

        return asyncio.run(control())

    def _get_music_service(self) -> MusicAssistantService:
        """Get a MusicAssistantService instance"""
        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        ma_token = get_secret_value("MUSIC_ASSISTANT_TOKEN", "integration")
        return MusicAssistantService(ma_url, ma_token)

    async def _resolve_player(
        self,
        service: MusicAssistantService,
        player_name: Optional[str],
        node_id: str
    ) -> Optional[str]:
        """Resolve player name to player ID"""
        if player_name:
            player = await service.get_player_by_name(player_name)
            return player["id"] if player else None
        return None

    def _get_default_player(self, node_id: str) -> Optional[str]:
        """Get the default player for the node's room"""
        # TODO: Query Command Center for room's default speaker
        return None

    async def _execute_action(
        self,
        service: MusicAssistantService,
        player_id: str,
        action: str,
        volume_level: Optional[int] = None
    ) -> None:
        """Execute the playback action"""
        if action == "pause":
            await service.pause(player_id)
        elif action == "resume":
            await service.resume(player_id)
        elif action == "stop":
            await service.stop(player_id)
        elif action == "next":
            await service.next_track(player_id)
        elif action == "previous":
            await service.previous_track(player_id)
        elif action == "volume_up":
            await service.volume_up(player_id)
        elif action == "volume_down":
            await service.volume_down(player_id)
        elif action == "volume_set":
            if volume_level is not None:
                await service.set_volume(player_id, volume_level)
        elif action == "mute":
            await service.set_volume(player_id, 0)
        elif action == "unmute":
            # Unmute to a reasonable default
            await service.set_volume(player_id, 50)
        elif action == "shuffle_on":
            await service.set_shuffle(player_id, True)
        elif action == "shuffle_off":
            await service.set_shuffle(player_id, False)
        elif action == "repeat_off":
            await service.set_repeat(player_id, RepeatMode.OFF)
        elif action == "repeat_one":
            await service.set_repeat(player_id, RepeatMode.ONE)
        elif action == "repeat_all":
            await service.set_repeat(player_id, RepeatMode.ALL)

    def _build_confirmation_message(self, action: str, volume_level: Optional[int]) -> str:
        """Build a confirmation message for the action"""
        messages = {
            "pause": "Music paused",
            "resume": "Music resumed",
            "stop": "Music stopped",
            "next": "Skipped to next track",
            "previous": "Went back to previous track",
            "volume_up": "Volume increased",
            "volume_down": "Volume decreased",
            "volume_set": f"Volume set to {volume_level}" if volume_level else "Volume set",
            "mute": "Audio muted",
            "unmute": "Audio unmuted",
            "shuffle_on": "Shuffle enabled",
            "shuffle_off": "Shuffle disabled",
            "repeat_off": "Repeat disabled",
            "repeat_one": "Repeating current track",
            "repeat_all": "Repeating entire queue",
        }
        return messages.get(action, f"Action {action} completed")
