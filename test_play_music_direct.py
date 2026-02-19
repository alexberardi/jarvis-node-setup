#!/usr/bin/env python3
"""
Direct test for Music Assistant integration.

Bypasses Command Center and tests the service layer directly.

Usage:
    python test_play_music_direct.py "Beatles" "Office"
    python test_play_music_direct.py "some jazz"  # uses first available player
    python test_play_music_direct.py --list-players
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add repo root to path
REPO_ROOT = Path(__file__).resolve().parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv
load_dotenv()

from services.secret_service import get_secret_value
from services.music_assistant_service import MusicAssistantService, MediaType


async def list_players(service: MusicAssistantService) -> list[dict]:
    """List all available players."""
    await service.connect()
    players = await service.get_players()
    await service.disconnect()
    return players


async def play_music(
    service: MusicAssistantService,
    query: str,
    player_name: str | None = None,
    media_type: str | None = None
) -> dict:
    """Search and play music."""
    await service.connect()

    # Find player
    if player_name:
        player = await service.get_player_by_name(player_name)
        if not player:
            await service.disconnect()
            return {"success": False, "error": f"Player '{player_name}' not found"}
    else:
        players = await service.get_players()
        if not players:
            await service.disconnect()
            return {"success": False, "error": "No players found"}
        player = players[0]
        print(f"Using player: {player['name']}")

    # Map media type
    mt = None
    if media_type:
        mt_map = {
            "track": MediaType.TRACK,
            "album": MediaType.ALBUM,
            "artist": MediaType.ARTIST,
            "playlist": MediaType.PLAYLIST,
            "radio": MediaType.RADIO,
        }
        mt = mt_map.get(media_type.lower())

    # Search and play
    result = await service.search_and_play(
        query=query,
        queue_id=player["id"],
        media_type=mt
    )

    await service.disconnect()
    return result


def main():
    parser = argparse.ArgumentParser(description="Test Music Assistant directly")
    parser.add_argument("query", nargs="?", help="What to play (e.g., 'Beatles', 'some jazz')")
    parser.add_argument("player", nargs="?", help="Player name (optional)")
    parser.add_argument("--type", "-t", choices=["track", "album", "artist", "playlist", "radio"],
                        help="Media type filter")
    parser.add_argument("--list-players", "-l", action="store_true", help="List available players")

    args = parser.parse_args()

    # Get credentials
    url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
    token = get_secret_value("MUSIC_ASSISTANT_TOKEN", "integration")

    if not url or not token:
        print("Error: Music Assistant not configured.")
        print("Run: python scripts/init_data.py --command play_music")
        sys.exit(1)

    service = MusicAssistantService(url, token)

    if args.list_players:
        players = asyncio.run(list_players(service))
        print(f"\nAvailable players ({len(players)}):\n")
        for p in players:
            print(f"  • {p['name']} ({p['state']})")
            print(f"    ID: {p['id']}")
        sys.exit(0)

    if not args.query:
        parser.print_help()
        sys.exit(1)

    print(f"\n🎵 Playing: {args.query}")
    if args.player:
        print(f"📍 Player: {args.player}")
    if args.type:
        print(f"📀 Type: {args.type}")

    result = asyncio.run(play_music(service, args.query, args.player, args.type))

    if result.get("success"):
        item = result.get("item", {})
        print(f"\n✓ Now playing: {item.get('name', 'Unknown')} ({item.get('type', 'unknown')})")
    else:
        print(f"\n✗ Failed: {result.get('error', 'Unknown error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
