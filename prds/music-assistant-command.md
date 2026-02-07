# Music Assistant Command

## Overview

Add voice control for Music Assistant via two new commands: `play_music` and `control_music`. Also extend `IJarvisCommand` to support package dependencies for third-party command distribution.

## Goals

1. Play music by artist, album, track, playlist, or radio/genre
2. Control playback: pause, resume, skip, volume, shuffle, repeat
3. Smart player selection using room context (from Command Center)
4. Support third-party commands with custom package dependencies

## Non-Goals

- Queue management UI
- Multi-room sync control (future)
- Playlist creation/editing

---

## Part 1: Command Initialization Hook

### Problem

Some commands need to sync data on first install - register devices, fetch initial state, set up integrations. Currently there's no standard way to do this.

### Solution

Add optional `init_data()` method to `IJarvisCommand`:

```python
# core/ijarvis_command.py (addition)

class IJarvisCommand(ABC):
    # ... existing properties ...

    def init_data(self) -> dict[str, Any]:
        """
        Optional initialization hook. Called manually on first install.
        Use for: syncing devices to Command Center, fetching initial state, etc.

        Returns:
            dict with initialization results (for logging/display)
        """
        return {"status": "no_init_required"}
```

### Runner Script

```python
# scripts/init_data.py

"""
Initialize data for a specific command.
Usage: python scripts/init_data.py --command play_music
"""

import argparse
from utils.command_discovery_service import discover_commands


def main():
    parser = argparse.ArgumentParser(description="Initialize command data")
    parser.add_argument("--command", required=True, help="Command name to initialize")
    args = parser.parse_args()

    commands = discover_commands()
    command = next((c for c in commands if c.command_name == args.command), None)

    if not command:
        print(f"Command '{args.command}' not found")
        return 1

    if not hasattr(command, 'init_data'):
        print(f"Command '{args.command}' has no init_data method")
        return 0

    print(f"Initializing {args.command}...")
    result = command.init_data()
    print(f"Result: {result}")
    return 0


if __name__ == "__main__":
    exit(main())
```

### Music Command init_data()

```python
# In PlayMusicCommand

def init_data(self) -> dict[str, Any]:
    """Sync Music Assistant players to Command Center devices table."""
    import asyncio
    from services.music_assistant_service import MusicAssistantService
    from clients.command_center_client import CommandCenterClient

    ma_url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
    if not ma_url:
        return {"status": "error", "message": "MUSIC_ASSISTANT_URL not configured"}

    async def sync():
        ma = MusicAssistantService(ma_url)
        await ma.connect()

        players = await ma.get_players()

        cc = CommandCenterClient()
        synced = 0

        for player in players:
            await cc.upsert_device({
                "name": player["name"],
                "type": "speaker",
                "metadata": {
                    "source": "music_assistant",
                    "ma_player_id": player["id"],
                }
            })
            synced += 1

        await ma.disconnect()
        return synced

    count = asyncio.run(sync())
    return {"status": "success", "devices_synced": count}
```

### Usage

```bash
# On first install, after setting MUSIC_ASSISTANT_URL secret:
python scripts/init_data.py --command play_music

# Output:
# Initializing play_music...
# Result: {'status': 'success', 'devices_synced': 4}
```

---

## Part 2: Package Dependencies

### Problem

Third-party command developers need to specify pip dependencies. Currently there's no mechanism for this - they'd have to say "add this to requirements.txt manually."

### Solution

Add `required_packages` property to `IJarvisCommand`:

```python
# core/ijarvis_package.py

from dataclasses import dataclass
from typing import Optional


@dataclass
class JarvisPackage:
    """Python package dependency for a command."""
    name: str                          # PyPI package name
    version: Optional[str] = None      # Version spec: "1.2.3", ">=1.0,<2.0", etc.

    def to_pip_spec(self) -> str:
        """Convert to pip install specification."""
        if not self.version:
            return self.name
        if self.version[0].isdigit():
            return f"{self.name}=={self.version}"
        return f"{self.name}{self.version}"
```

```python
# core/ijarvis_command.py (addition)

from .ijarvis_package import JarvisPackage

class IJarvisCommand(ABC):
    # ... existing properties ...

    @property
    def required_packages(self) -> List[JarvisPackage]:
        """
        Python packages this command requires.
        Installed on first use, written to custom-requirements.txt.
        """
        return []
```

### Package Installation (Current Approach)

For now, command-specific packages are manually added:

1. Add packages to `custom-requirements.txt` (gitignored)
2. Reference in `setup.sh` to install alongside other dependencies

```bash
# setup.sh addition
if [ -f custom-requirements.txt ]; then
    pip install -r custom-requirements.txt
fi
```

```
# custom-requirements.txt (gitignored)
music-assistant-client==1.0.0
```

**Future consideration**: When third-party commands are supported, we'll need a secure dependency installation flow. Deferred for now due to security implications (arbitrary pip installs).

---

## Part 2: Music Assistant Commands

### Dependencies

```python
@property
def required_packages(self) -> List[JarvisPackage]:
    return [
        JarvisPackage("music-assistant-client", "1.0.0"),  # Pin to stable version
    ]
```

Note: `music-assistant-models` is a dependency of the client, installed automatically.

### Command 1: `play_music`

**Purpose**: Search for and play music content.

```python
class PlayMusicCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "play_music"

    @property
    def description(self) -> str:
        return "Play music - search for artists, albums, songs, playlists, or radio stations"

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
                "query", "string", required=True,
                description="What to play: artist name, album, song, playlist, or genre"
            ),
            JarvisParameter(
                "media_type", "string", required=False,
                enum_values=["track", "album", "artist", "playlist", "radio"],
                description="Type of content. Infer from context if not specified."
            ),
            JarvisParameter(
                "player", "string", required=False,
                description="Target speaker name. If not specified, use room's default speaker."
            ),
            JarvisParameter(
                "queue_option", "string", required=False,
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
                "integration", "string"
            ),
            JarvisSecret(
                "MUSIC_QUEUE_BEHAVIOR",
                "When music is playing: 'replace' (default), 'ask', or 'add'",
                "node", "string",
                required=False
            ),
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "If user specifies a speaker/player, use the 'player' parameter",
            "If no player specified, the room's default speaker will be used",
            "For 'play some jazz' or genre requests, use media_type='radio'",
            "For 'play [artist name]', use media_type='artist' to play their catalog",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                "control_music",
                "Playback controls: pause, stop, skip, volume, shuffle. No content search."
            )
        ]
```

**Example Mappings:**

| Voice Command | Parameters |
|--------------|------------|
| "Play Radiohead" | `{query: "Radiohead", media_type: "artist"}` |
| "Play OK Computer" | `{query: "OK Computer", media_type: "album"}` |
| "Play Karma Police" | `{query: "Karma Police", media_type: "track"}` |
| "Play some jazz" | `{query: "jazz", media_type: "radio"}` |
| "Play Taylor Swift in the kitchen" | `{query: "Taylor Swift", media_type: "artist", player: "Kitchen Echo"}` |
| "Queue up Bohemian Rhapsody" | `{query: "Bohemian Rhapsody", queue_option: "add"}` |

### Command 2: `control_music`

**Purpose**: Control playback without searching for content.

```python
class ControlMusicCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "control_music"

    @property
    def description(self) -> str:
        return "Control music playback: pause, resume, skip, volume, shuffle, repeat"

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
                "action", "string", required=True,
                enum_values=[
                    "pause", "resume", "stop", "next", "previous",
                    "shuffle_on", "shuffle_off",
                    "repeat_off", "repeat_one", "repeat_all",
                    "volume_up", "volume_down", "volume_set", "mute", "unmute"
                ],
                description="The playback action to perform"
            ),
            JarvisParameter(
                "volume_level", "int", required=False,
                description="Volume level 0-100. Only used with volume_set action."
            ),
            JarvisParameter(
                "player", "string", required=False,
                description="Target speaker. If not specified, controls current/default player."
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                "MUSIC_ASSISTANT_URL",
                "Music Assistant WebSocket URL",
                "integration", "string"
            ),
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use 'resume' not 'play' when continuing paused music",
            "For 'turn up the volume' use 'volume_up'",
            "For 'set volume to 50' use 'volume_set' with volume_level=50",
            "For 'louder'/'quieter' use volume_up/volume_down",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                "play_music",
                "Playing specific content: artists, songs, albums, playlists, genres"
            )
        ]
```

**Example Mappings:**

| Voice Command | Parameters |
|--------------|------------|
| "Pause" / "Pause the music" | `{action: "pause"}` |
| "Resume" / "Continue" | `{action: "resume"}` |
| "Skip" / "Next song" | `{action: "next"}` |
| "Go back" / "Previous" | `{action: "previous"}` |
| "Turn it up" | `{action: "volume_up"}` |
| "Set volume to 30" | `{action: "volume_set", volume_level: 30}` |
| "Shuffle on" | `{action: "shuffle_on"}` |
| "Repeat this song" | `{action: "repeat_one"}` |

---

## Part 3: Music Assistant Client Wrapper

The existing `utils/music_assistant_service.py` is outdated. Replace with a wrapper around the official client:

```python
# services/music_assistant_service.py

from typing import Optional, List, Any
from music_assistant_client import MusicAssistantClient
from music_assistant_models.enums import MediaType, QueueOption, RepeatMode


class MusicAssistantService:
    """Wrapper around official Music Assistant client."""

    def __init__(self, url: str):
        self.url = url
        self._client: Optional[MusicAssistantClient] = None

    async def connect(self) -> None:
        """Establish connection to Music Assistant."""
        self._client = MusicAssistantClient(self.url)
        await self._client.start_listening()

    async def disconnect(self) -> None:
        """Close connection."""
        if self._client:
            await self._client.disconnect()

    # --- Playback ---

    async def search_and_play(
        self,
        query: str,
        queue_id: str,
        media_type: Optional[MediaType] = None,
        queue_option: QueueOption = QueueOption.PLAY,
        radio_mode: bool = False
    ) -> dict:
        """Search for content and play it."""
        # Search
        media_types = [media_type] if media_type else list(MediaType)
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

    # --- Controls ---

    async def pause(self, queue_id: str) -> None:
        await self._client.player_queues.pause(queue_id)

    async def resume(self, queue_id: str) -> None:
        await self._client.player_queues.resume(queue_id)

    async def stop(self, queue_id: str) -> None:
        await self._client.player_queues.stop(queue_id)

    async def next_track(self, queue_id: str) -> None:
        await self._client.player_queues.next(queue_id)

    async def previous_track(self, queue_id: str) -> None:
        await self._client.player_queues.previous(queue_id)

    async def set_volume(self, player_id: str, level: int) -> None:
        await self._client.players.volume_set(player_id, level)

    async def volume_up(self, player_id: str) -> None:
        await self._client.players.volume_up(player_id)

    async def volume_down(self, player_id: str) -> None:
        await self._client.players.volume_down(player_id)

    async def set_shuffle(self, queue_id: str, enabled: bool) -> None:
        await self._client.player_queues.shuffle(queue_id, enabled)

    async def set_repeat(self, queue_id: str, mode: RepeatMode) -> None:
        await self._client.player_queues.repeat(queue_id, mode)

    # --- Players ---

    async def get_players(self) -> List[dict]:
        """Get all available players."""
        players = await self._client.players.get_all()
        return [
            {"id": p.player_id, "name": p.display_name, "state": p.state.value}
            for p in players
        ]

    async def get_player_by_name(self, name: str) -> Optional[dict]:
        """Find player by display name (fuzzy match)."""
        players = await self.get_players()
        name_lower = name.lower()
        for p in players:
            if name_lower in p["name"].lower():
                return p
        return None

    # --- Helpers ---

    def _pick_best_result(self, results, preferred_type: Optional[MediaType]):
        """Pick best search result, preferring the specified type."""
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
```

---

## Part 4: Disambiguation Flow

When search returns multiple matches, the command returns context for the LLM to ask the user:

```python
def run(self, request_info, **kwargs) -> CommandResponse:
    results = self._search(query)

    if len(results) == 0:
        return CommandResponse.error_response(
            error_details=f"I couldn't find anything for '{query}'"
        )

    if len(results) == 1 or self._is_clear_match(results[0], query):
        # Clear winner - play it
        self._play(results[0])
        return CommandResponse.success_response(
            context_data={
                "action": "now_playing",
                "item": self._serialize(results[0])
            }
        )

    # Multiple matches - let LLM ask user
    return CommandResponse.follow_up_response(
        context_data={
            "action": "disambiguation_needed",
            "query": query,
            "candidates": [self._serialize(r) for r in results[:5]],
            "message": f"Found {len(results)} matches for '{query}'"
        }
    )
```

The LLM sees this in the tool result and generates a clarifying question. User responds, LLM calls the command again with the specific choice.

---

## Implementation Checklist

### IJarvisCommand Extensions
1. [ ] Add `init_data()` method to `IJarvisCommand` in `core/ijarvis_command.py`
2. [ ] Create `core/ijarvis_package.py` with `JarvisPackage` dataclass
3. [ ] Add `required_packages` property to `IJarvisCommand`
4. [ ] Create `scripts/init_data.py` runner script

### Package Setup
5. [ ] Add `music-assistant-client` to `custom-requirements.txt` (gitignored)
6. [ ] Update `setup.sh` to install from `custom-requirements.txt`

### Music Assistant Integration
7. [ ] Replace `utils/music_assistant_service.py` with new async wrapper
8. [ ] Create `commands/play_music_command.py`
9. [ ] Create `commands/control_music_command.py`
10. [ ] Implement `init_data()` in PlayMusicCommand for device sync
11. [ ] Add `MUSIC_ASSISTANT_URL` to secrets (integration scope)
12. [ ] Add `MUSIC_QUEUE_BEHAVIOR` to secrets (node scope, optional)

### Testing
13. [ ] Add adapter training examples for both commands
14. [ ] Test with local Music Assistant instance
15. [ ] Run `python scripts/init_data.py --command play_music` to sync devices

---

## Dependencies

- **Command Center**: `home-room-context.md` for device/room context
- **Music Assistant**: Running instance with API access
- **PyPI**: `music-assistant-client` package

## Testing

```bash
# E2E test with running services
python test_command_parsing.py -c play_music control_music
```
