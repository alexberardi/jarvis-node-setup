#!/usr/bin/env python3
"""
Music command for Jarvis.

Unified command for all music operations: play content (search + play)
and control playback (pause, resume, skip, volume, shuffle, repeat).
The 'action' parameter is the discriminator:
  - action='play' → search for content and play it
  - all other actions → control existing playback
"""

import asyncio
import re
from typing import Any, Dict, List, Optional

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand, PreRouteResult
from core.ijarvis_package import JarvisPackage
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation
from services.music_assistant_service import (
    MediaType,
    MusicAssistantService,
    QueueOption,
    RepeatMode,
)
from services.secret_service import get_secret_value


# Actions the LLM sees in the enum (collapsed for 3B accuracy)
LLM_ACTIONS = sorted([
    "play", "pause", "resume", "stop", "next", "previous",
    "volume_up", "volume_down", "volume_set", "mute", "unmute",
    "shuffle", "repeat",
])

# All actions that run() accepts (specific + collapsed aliases)
CONTROL_ACTIONS = {
    "pause", "resume", "stop", "next", "previous",
    "shuffle_on", "shuffle_off", "shuffle",
    "repeat_off", "repeat_one", "repeat_all", "repeat",
    "volume_up", "volume_down", "volume_set", "mute", "unmute",
}

ALL_ACTIONS = {"play"} | CONTROL_ACTIONS

# --- Pre-route patterns (deterministic, bypass LLM) ---
_MAX_PRE_ROUTE_WORDS = 5
_VOLUME_RE = re.compile(r"^(?:set )?volume (?:to )?(\d+)$")
_PLAY_NEXT_RE = re.compile(r"^play (.+?) (?:next|after this|after that)$")
_SKIP_RE = re.compile(r"^skip(?: this)?(?: song| track)?$")
_CONTROL_PLAYER_RE = re.compile(
    r"^(pause|resume|stop|mute|unmute) (?:the |my )?(.+?)(?:\s+(?:speaker|player))?$"
)

# --- Post-process patterns (fix LLM tool call args) ---
_PLAY_PREFIXES = re.compile(
    r"^(?:play|put on|throw on|listen to)\s+(?:some\s+|a\s+|the\s+)?",
    re.IGNORECASE,
)


class MusicCommand(IJarvisCommand):
    """Unified command for playing and controlling music via Music Assistant"""

    @property
    def command_name(self) -> str:
        return "music"

    @property
    def description(self) -> str:
        return (
            "'stop repeating'=repeat, 'repeat this'=repeat, 'go back'=previous, "
            "'louder'=volume_up, 'quieter'=volume_down. "
            "Music: play content or control playback. "
            "action='play' to search+play. Other actions control existing playback."
        )

    @property
    def associated_service(self) -> str | None:
        return "Music Assistant"

    @property
    def keywords(self) -> List[str]:
        return [
            "play", "music", "song", "album", "artist", "playlist",
            "listen", "put on", "throw on", "queue", "radio",
            "pause", "stop", "resume", "skip", "next", "previous",
            "volume", "louder", "quieter", "mute", "shuffle", "repeat",
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "action",
                "string",
                required=True,
                enum_values=LLM_ACTIONS,
                description=(
                    "play=search+play content, pause/resume/stop=transport, "
                    "next/previous=track nav, louder→volume_up, quieter→volume_down, "
                    "go back→previous, repeat=toggle repeat, shuffle=toggle shuffle, "
                    "mute/silence→mute"
                ),
            ),
            JarvisParameter(
                "query",
                "string",
                required=False,
                description="What to play (only for action='play'): artist, album, song, playlist, or genre",
            ),
            JarvisParameter(
                "player",
                "string",
                required=False,
                description="Target speaker name",
            ),
            JarvisParameter(
                "queue_option",
                "string",
                required=False,
                enum_values=["play", "next", "add"],
                description="'play' replaces queue (default), 'next' plays after current, 'add' appends",
                refinable=True,
            ),
            JarvisParameter(
                "volume_level",
                "int",
                required=False,
                description="Volume level 0-100. Only for action='volume_set'.",
                refinable=True,
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "MUSIC_ASSISTANT_URL",
                "Music Assistant WebSocket URL (e.g., ws://192.168.1.50:8095/ws)",
                "integration",
                "string",
                is_sensitive=False,
            ),
            JarvisSecret(
                "MUSIC_ASSISTANT_TOKEN",
                "Music Assistant auth token (run test_music_assistant.py --login to generate)",
                "integration",
                "string",
            ),
            JarvisSecret(
                "MUSIC_ASSISTANT_PLAYER_ID",
                "Default player ID (MAC address, e.g., a6:f5:c8:63:26:b5)",
                "integration",
                "string",
                is_sensitive=False,
                friendly_name="Default Player",
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
            "If user says 'queue' or 'add to queue', use action='play', queue_option='add'",
            "Use 'resume' not 'play' when continuing paused music",
            "For 'turn up the volume' use action='volume_up'",
            "For 'set volume to 50' use action='volume_set' with volume_level=50",
            "For 'louder'/'quieter' use volume_up/volume_down",
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use 'resume' not 'play' for unpausing.",
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="Play Radiohead",
                expected_parameters={"action": "play", "query": "Radiohead"},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Play some jazz",
                expected_parameters={"action": "play", "query": "jazz"},
            ),
            CommandExample(
                voice_command="Pause the music",
                expected_parameters={"action": "pause"},
            ),
            CommandExample(
                voice_command="Go back",
                expected_parameters={"action": "previous"},
            ),
            CommandExample(
                voice_command="Turn up the volume",
                expected_parameters={"action": "volume_up"},
            ),
            CommandExample(
                voice_command="Repeat this song",
                expected_parameters={"action": "repeat"},
            ),
            CommandExample(
                voice_command="Stop repeating",
                expected_parameters={"action": "repeat"},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        items = [
            # Play: various content types (Music Assistant resolves media type)
            ("Play Radiohead", {"action": "play", "query": "Radiohead"}),
            ("Put on some Beatles", {"action": "play", "query": "Beatles"}),
            ("Listen to Taylor Swift", {"action": "play", "query": "Taylor Swift"}),
            ("Play some Daft Punk", {"action": "play", "query": "Daft Punk"}),
            ("Play OK Computer", {"action": "play", "query": "OK Computer"}),
            ("Play the album Abbey Road", {"action": "play", "query": "Abbey Road"}),
            ("Put on Dark Side of the Moon", {"action": "play", "query": "Dark Side of the Moon"}),
            ("Play Karma Police", {"action": "play", "query": "Karma Police"}),
            ("Play the song Bohemian Rhapsody", {"action": "play", "query": "Bohemian Rhapsody"}),
            ("Put on Hey Jude", {"action": "play", "query": "Hey Jude"}),
            ("Play some jazz", {"action": "play", "query": "jazz"}),
            ("Put on classical music", {"action": "play", "query": "classical"}),
            ("Play relaxing music", {"action": "play", "query": "relaxing"}),
            ("Play some 80s hits", {"action": "play", "query": "80s hits"}),
            # Play: with player
            ("Play Taylor Swift in the kitchen", {
                "action": "play", "query": "Taylor Swift", "player": "Kitchen Echo",
            }),
            ("Play jazz in the living room", {
                "action": "play", "query": "jazz", "player": "Living Room Sonos",
            }),
            # Play: queue options
            ("Queue up Bohemian Rhapsody", {
                "action": "play", "query": "Bohemian Rhapsody", "queue_option": "add",
            }),
            ("Play Stairway to Heaven next", {
                "action": "play", "query": "Stairway to Heaven", "queue_option": "next",
            }),
            # Control: transport
            ("Pause", {"action": "pause"}),
            ("Pause the music", {"action": "pause"}),
            ("Resume", {"action": "resume"}),
            ("Continue", {"action": "resume"}),
            ("Unpause", {"action": "resume"}),
            ("Skip", {"action": "next"}),
            ("Skip this song", {"action": "next"}),
            ("Next song", {"action": "next"}),
            ("Go back", {"action": "previous"}),
            ("Previous song", {"action": "previous"}),
            # Volume
            ("Turn it up", {"action": "volume_up"}),
            ("Louder", {"action": "volume_up"}),
            ("Turn it down", {"action": "volume_down"}),
            ("Quieter", {"action": "volume_down"}),
            ("Set volume to 30", {"action": "volume_set", "volume_level": 30}),
            ("Volume 50", {"action": "volume_set", "volume_level": 50}),
            ("Mute", {"action": "mute"}),
            ("Unmute", {"action": "unmute"}),
            # Mode: shuffle/repeat
            ("Shuffle on", {"action": "shuffle"}),
            ("Turn on shuffle", {"action": "shuffle"}),
            ("Shuffle off", {"action": "shuffle"}),
            ("Repeat this song", {"action": "repeat"}),
            ("Repeat all", {"action": "repeat"}),
            ("Stop repeating", {"action": "repeat"}),
            # Control: with player
            ("Pause the kitchen speaker", {"action": "pause", "player": "Kitchen Speaker"}),
            ("Turn up the volume in the bedroom", {"action": "volume_up", "player": "Bedroom Speaker"}),
        ]
        examples = []
        for i, (utterance, params) in enumerate(items):
            examples.append(CommandExample(
                voice_command=utterance,
                expected_parameters=params,
                is_primary=(i == 0),
            ))
        return examples

    # ------------------------------------------------------------------
    # Pre-routing & post-processing (node-side, bypass LLM)
    # ------------------------------------------------------------------

    def pre_route(self, voice_command: str) -> PreRouteResult | None:
        text = voice_command.lower().strip()

        # Exact-match patterns (most specific) — only for short utterances
        params: Dict[str, Any] | None = None
        if len(voice_command.split()) <= _MAX_PRE_ROUTE_WORDS:
            params = self._match_exact(text)

        # Regex patterns are safe at any length
        if params is None:
            params = self._match_regex(voice_command.strip())

        if params is None:
            return None

        return PreRouteResult(arguments=params)

    @staticmethod
    def _match_exact(text: str) -> Dict[str, Any] | None:
        # Transport
        if text in ("resume", "unpause", "continue playing"):
            return {"action": "resume"}
        if text in ("go back", "previous song", "previous track", "last song"):
            return {"action": "previous"}

        # Volume
        if text in ("louder", "turn it up", "crank it up"):
            return {"action": "volume_up"}
        if text in ("quieter", "turn it down"):
            return {"action": "volume_down"}

        # Repeat modes
        if text in ("stop repeating", "don't repeat", "repeat off"):
            return {"action": "repeat_off"}
        if text in ("repeat this song", "repeat this track", "repeat this"):
            return {"action": "repeat_one"}
        if text in ("repeat all", "repeat the album", "repeat the queue"):
            return {"action": "repeat_all"}

        # Shuffle modes
        if text in ("shuffle off",):
            return {"action": "shuffle_off"}
        if text in ("shuffle on", "turn on shuffle", "shuffle"):
            return {"action": "shuffle_on"}

        return None

    @staticmethod
    def _match_regex(original: str) -> Dict[str, Any] | None:
        text = original.lower()

        # "Play X next/after this" → action=play, queue_option=next
        m = _PLAY_NEXT_RE.match(text)
        if m:
            # Capture query from the original casing
            start, end = m.start(1), m.end(1)
            return {"action": "play", "query": original[start:end], "queue_option": "next"}

        # "Set volume to N" / "Volume 50"
        m = _VOLUME_RE.match(text)
        if m:
            return {"action": "volume_set", "volume_level": int(m.group(1))}

        # "Skip", "Skip this song"
        m = _SKIP_RE.match(text)
        if m:
            return {"action": "next"}

        # "Pause the kitchen speaker"
        m = _CONTROL_PLAYER_RE.match(text)
        if m:
            action = m.group(1)
            start, end = m.start(2), m.end(2)
            player = original[start:end].strip()
            if player and player.lower() not in ("music", "playback", "audio", "it"):
                return {"action": action, "player": player}

        return None

    def post_process_tool_call(self, args: Dict[str, Any], voice_command: str) -> Dict[str, Any]:
        if args.get("action") != "play" or args.get("query"):
            return args

        # action=play but no query — extract from utterance
        stripped = _PLAY_PREFIXES.sub("", voice_command).strip()
        if stripped and stripped.lower() != voice_command.lower():
            args["query"] = stripped

        return args

    # ------------------------------------------------------------------
    # Init / setup
    # ------------------------------------------------------------------

    def init_data(self) -> Dict[str, Any]:
        """Setup Music Assistant integration."""
        import getpass
        from services.music_assistant_service import login_with_token
        from services.secret_service import set_secret

        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        ma_token = get_secret_value("MUSIC_ASSISTANT_TOKEN", "integration")

        # Step 1: Get URL if not configured
        if not ma_url:
            print("\n=== Music Assistant Setup ===\n")
            ma_url = input("Music Assistant URL (e.g., ws://10.0.0.244:8095/ws): ").strip()
            if not ma_url:
                return {"status": "error", "message": "URL is required"}
            if not ma_url.startswith("ws://") and not ma_url.startswith("wss://"):
                return {"status": "error", "message": "URL must start with ws:// or wss://"}
            set_secret("MUSIC_ASSISTANT_URL", ma_url, "integration", "string")
            print(f"Saved URL: {ma_url}")

        # Step 2: Get token if not configured
        if not ma_token:
            print("\n=== Music Assistant Authentication ===\n")
            print(f"Server: {ma_url}")
            print("\nOptions:")
            print("  1. Enter a Long Lived Token (create in Music Assistant UI)")
            print("  2. Login with username/password")
            print()
            choice = input("Choice [1/2]: ").strip()

            if choice == "1":
                ma_token = input("Paste your token: ").strip()
                if not ma_token:
                    return {"status": "error", "message": "Token is required"}
                set_secret("MUSIC_ASSISTANT_TOKEN", ma_token, "integration", "string")
                print("Token saved")
            else:
                username = input("Username: ").strip()
                password = getpass.getpass("Password: ")
                if not username or not password:
                    return {"status": "error", "message": "Username and password are required"}
                http_url = ma_url.replace("ws://", "http://").replace("wss://", "https://")
                if http_url.endswith("/ws"):
                    http_url = http_url[:-3]

                async def do_login():
                    return await login_with_token(http_url, username, password, "jarvis")

                try:
                    user, ma_token = asyncio.run(do_login())
                    set_secret("MUSIC_ASSISTANT_TOKEN", ma_token, "integration", "string")
                    print(f"Logged in as: {user.get('name', username)}")
                    print("Auth token saved")
                except Exception as e:
                    return {"status": "error", "message": f"Login failed: {e}"}

        # Step 3: Test connection and list players
        async def test_and_list():
            service = MusicAssistantService(ma_url, ma_token)
            try:
                await service.connect()
                players = await service.get_players()
                await service.disconnect()
                return players
            except Exception as e:
                try:
                    await service.disconnect()
                except Exception:
                    pass
                raise e

        try:
            players = asyncio.run(test_and_list())
            print(f"\n=== Available Players ({len(players)}) ===\n")
            for p in players:
                print(f"  - {p['name']} ({p['state']})")
            return {
                "status": "success",
                "players_found": len(players),
                "players": [p["name"] for p in players],
                "message": f"Music Assistant configured with {len(players)} player(s)",
            }
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the music command — routes to play or control logic."""
        action = kwargs.get("action")
        if not action:
            return CommandResponse.error_response(
                error_details="Action is required - what would you like to do?",
                context_data={"error": "missing_action"},
            )

        if action == "play":
            return self._run_play(request_info, **kwargs)

        if action not in CONTROL_ACTIONS:
            return CommandResponse.error_response(
                error_details=f"Invalid action '{action}'.",
                context_data={"error": "invalid_action", "action": action},
            )

        # Map collapsed enum values to specific defaults
        if action == "repeat":
            action = "repeat_one"
        elif action == "shuffle":
            action = "shuffle_on"

        control_kwargs = {k: v for k, v in kwargs.items() if k != "action"}
        return self._run_control(request_info, action, **control_kwargs)

    # ------------------------------------------------------------------
    # Play logic (search + play content)
    # ------------------------------------------------------------------

    def _run_play(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        query = kwargs.get("query")
        media_type_str = kwargs.get("media_type")
        player_name = kwargs.get("player")
        queue_option_str = kwargs.get("queue_option", "play")

        if not query:
            return CommandResponse.error_response(
                error_details="Query is required - what would you like to play?",
                context_data={"error": "missing_query"},
            )

        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        if not ma_url:
            return CommandResponse.error_response(
                error_details="Music Assistant is not configured",
                context_data={"error": "not_configured"},
            )

        media_type = self._parse_media_type(media_type_str)
        queue_option = self._parse_queue_option(queue_option_str)

        async def play():
            service = self._get_music_service()
            try:
                await service.connect()

                player_id = await self._resolve_player(service, player_name)
                if player_id is None and player_name:
                    await service.disconnect()
                    return CommandResponse.error_response(
                        error_details=f"I don't see a speaker called '{player_name}'",
                        context_data={"error": "player_not_found", "player": player_name},
                    )

                if player_id is None:
                    player_id = self._get_default_player()
                    if player_id is None:
                        await service.disconnect()
                        return CommandResponse.error_response(
                            error_details="No default speaker configured",
                            context_data={"error": "no_default_player"},
                        )

                result = await service.search_and_play(
                    query=query, queue_id=player_id,
                    media_type=media_type, queue_option=queue_option,
                )
                await service.disconnect()

                if result["success"]:
                    return CommandResponse.success_response(
                        context_data={
                            "action": "now_playing",
                            "item": result["item"],
                            "query": query,
                            "player_id": player_id,
                            "message": f"Now playing {result['item']['name']}",
                        },
                        wait_for_input=False,
                    )
                return CommandResponse.error_response(
                    error_details=f"No results found for '{query}'",
                    context_data={"error": "no_results", "query": query},
                )
            except Exception as e:
                await service.disconnect()
                return CommandResponse.error_response(
                    error_details=f"Music playback error: {str(e)}",
                    context_data={"error": str(e)},
                )

        return asyncio.run(play())

    # ------------------------------------------------------------------
    # Control logic (transport, volume, shuffle, repeat)
    # ------------------------------------------------------------------

    def _run_control(self, request_info: RequestInformation, action: str, **kwargs) -> CommandResponse:
        volume_level = kwargs.get("volume_level")
        player_name = kwargs.get("player")

        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        if not ma_url:
            return CommandResponse.error_response(
                error_details="Music Assistant is not configured",
                context_data={"error": "not_configured"},
            )

        async def control():
            service = self._get_music_service()
            try:
                await service.connect()

                player_id = await self._resolve_player(service, player_name)
                if player_id is None and player_name:
                    await service.disconnect()
                    return CommandResponse.error_response(
                        error_details=f"I don't see a speaker called '{player_name}'",
                        context_data={"error": "player_not_found", "player": player_name},
                    )

                if player_id is None:
                    player_id = self._get_default_player()
                    if player_id is None:
                        await service.disconnect()
                        return CommandResponse.error_response(
                            error_details="No default speaker configured",
                            context_data={"error": "no_default_player"},
                        )

                await self._execute_action(service, player_id, action, volume_level)
                await service.disconnect()

                return CommandResponse.success_response(
                    context_data={
                        "action": action,
                        "player_id": player_id,
                        "volume_level": volume_level,
                        "message": self._build_confirmation_message(action, volume_level),
                    },
                    wait_for_input=False,
                )
            except Exception as e:
                await service.disconnect()
                return CommandResponse.error_response(
                    error_details=f"Music control error: {str(e)}",
                    context_data={"error": str(e)},
                )

        return asyncio.run(control())

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _get_music_service(self) -> MusicAssistantService:
        ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
        ma_token = get_secret_value("MUSIC_ASSISTANT_TOKEN", "integration")
        return MusicAssistantService(ma_url, ma_token)

    async def _resolve_player(
        self, service: MusicAssistantService, player_name: Optional[str],
    ) -> Optional[str]:
        if player_name:
            player = await service.get_player_by_name(player_name)
            return player["id"] if player else None
        return None

    def _get_default_player(self) -> Optional[str]:
        return get_secret_value("MUSIC_ASSISTANT_PLAYER_ID", "integration") or None

    def _parse_media_type(self, media_type_str: Optional[str]) -> Optional[MediaType]:
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
        mapping = {
            "play": QueueOption.PLAY,
            "next": QueueOption.NEXT,
            "add": QueueOption.ADD,
        }
        return mapping.get(
            queue_option_str.lower() if queue_option_str else "play",
            QueueOption.PLAY,
        )

    async def _execute_action(
        self, service: MusicAssistantService, player_id: str,
        action: str, volume_level: Optional[int] = None,
    ) -> None:
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
