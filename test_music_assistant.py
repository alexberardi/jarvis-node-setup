#!/usr/bin/env python3
"""
Music Assistant setup and test script.

Usage:
    # Login and save credentials (first time setup)
    python test_music_assistant.py ws://10.0.0.244:8095/ws --login

    # Test connection with saved credentials
    python test_music_assistant.py

    # Test connection with explicit URL (uses saved token)
    python test_music_assistant.py ws://10.0.0.244:8095/ws

    # Test search + play
    python test_music_assistant.py --play "Beatles" --player "Living Room"
"""

import argparse
import asyncio
import getpass
import sys

from services.music_assistant_service import MusicAssistantService, login_with_token


def get_saved_credentials() -> tuple[str | None, str | None]:
    """Get saved URL and token from secrets."""
    from services.secret_service import get_secret_value
    url = get_secret_value("MUSIC_ASSISTANT_URL", "integration")
    token = get_secret_value("MUSIC_ASSISTANT_TOKEN", "integration")
    return url, token


def save_credentials(url: str, token: str) -> None:
    """Save URL and token as secrets."""
    from services.secret_service import set_secret
    set_secret("MUSIC_ASSISTANT_URL", url, "integration", "string")
    set_secret("MUSIC_ASSISTANT_TOKEN", token, "integration", "string")
    print("✓ Credentials saved successfully")


async def do_login(url: str) -> str:
    """Interactive login to get a token."""
    # Convert ws:// URL to http:// for login
    http_url = url.replace("ws://", "http://").replace("wss://", "https://")
    if http_url.endswith("/ws"):
        http_url = http_url[:-3]

    print(f"\nLogin to Music Assistant at {http_url}")
    username = input("Username: ")
    password = getpass.getpass("Password: ")

    print("Authenticating...")
    user, token = await login_with_token(http_url, username, password, "jarvis")
    print(f"✓ Logged in as: {user.get('name', username)}")
    return token


async def test_connection(url: str, token: str | None) -> list[dict]:
    """Test connection to Music Assistant and list players."""
    print(f"Connecting to Music Assistant at {url}...")
    if token:
        print("  (using saved auth token)")

    service = MusicAssistantService(url, token)

    try:
        await service.connect()
        print("✓ Connected successfully!\n")

        players = await service.get_players()

        if players:
            print(f"Found {len(players)} player(s):\n")
            for i, player in enumerate(players, 1):
                print(f"  {i}. {player['name']}")
                print(f"     ID: {player['id']}")
                print(f"     State: {player['state']}")
                print()
        else:
            print("No players found. Make sure you have players configured in Music Assistant.")

        await service.disconnect()
        print("✓ Disconnected cleanly")
        return players

    except Exception as e:
        print(f"✗ Connection failed: {e}")
        raise


async def test_search_and_play(url: str, token: str | None, query: str, player_name: str) -> None:
    """Test search and play functionality."""
    print(f"\nTesting search and play...")
    print(f"  Query: {query}")
    print(f"  Player: {player_name}")

    service = MusicAssistantService(url, token)

    try:
        await service.connect()

        # Find player
        player = await service.get_player_by_name(player_name)
        if not player:
            print(f"✗ Player '{player_name}' not found")
            await service.disconnect()
            return

        print(f"  Found player: {player['name']} ({player['id']})")

        # Search and play
        result = await service.search_and_play(
            query=query,
            queue_id=player["id"],
            media_type=None  # Auto-detect
        )

        if result["success"]:
            print(f"✓ Now playing: {result['item']['name']} ({result['item']['type']})")
        else:
            print(f"✗ Failed: {result.get('error', 'Unknown error')}")

        await service.disconnect()

    except Exception as e:
        print(f"✗ Error: {e}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Test and configure Music Assistant connection"
    )
    parser.add_argument(
        "url",
        nargs="?",
        help="Music Assistant WebSocket URL (e.g., ws://10.0.0.244:8095/ws)"
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Login with username/password to get auth token"
    )
    parser.add_argument(
        "--play",
        metavar="QUERY",
        help="Test search and play with this query"
    )
    parser.add_argument(
        "--player",
        metavar="NAME",
        help="Player name for --play test"
    )

    args = parser.parse_args()

    # Get saved credentials
    saved_url, saved_token = get_saved_credentials()

    # Determine URL to use
    url = args.url or saved_url
    if not url:
        print("Error: No URL provided and no saved URL found.")
        print("Usage: python test_music_assistant.py ws://10.0.0.244:8095/ws --login")
        sys.exit(1)

    # Validate URL format
    if not url.startswith("ws://") and not url.startswith("wss://"):
        print("Error: URL must start with ws:// or wss://")
        print("Example: ws://10.0.0.244:8095/ws")
        sys.exit(1)

    token = saved_token

    try:
        # Login if requested
        if args.login:
            token = asyncio.run(do_login(url))
            save_credentials(url, token)

        # Test connection
        players = asyncio.run(test_connection(url, token))

        # Test play if requested
        if args.play:
            if not args.player:
                if players:
                    print(f"\n--player not specified. Available players:")
                    for p in players:
                        print(f"  - {p['name']}")
                    sys.exit(1)
                else:
                    print("Error: --play requires --player, but no players found")
                    sys.exit(1)

            asyncio.run(test_search_and_play(url, token, args.play, args.player))

    except Exception as e:
        print(f"\nFailed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
