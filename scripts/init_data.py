#!/usr/bin/env python3
"""
Initialize data for a specific command.

Runs the init_data() method on a command for first-install setup.
Used to sync devices, fetch initial state, or set up integrations.

Usage:
    python scripts/init_data.py --command play_music
"""

import argparse
import importlib
import pkgutil
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# Add repo root to path for imports
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.ijarvis_command import IJarvisCommand


def get_all_commands() -> Dict[str, IJarvisCommand]:
    """
    Discover all IJarvisCommand implementations.

    Returns:
        Dictionary mapping command names to command instances
    """
    import commands

    discovered = {}

    for _, module_name, _ in pkgutil.iter_modules(commands.__path__):
        try:
            module = importlib.import_module(f"commands.{module_name}")

            for attr in dir(module):
                cls = getattr(module, attr)

                if (isinstance(cls, type) and
                    issubclass(cls, IJarvisCommand) and
                    cls is not IJarvisCommand):

                    instance = cls()
                    discovered[instance.command_name] = instance

        except Exception as e:
            print(f"Warning: Error loading command module {module_name}: {e}")

    return discovered


def find_command(command_name: str) -> Optional[IJarvisCommand]:
    """
    Find a command by name.

    Args:
        command_name: The command name to find

    Returns:
        The command instance, or None if not found
    """
    commands = get_all_commands()
    return commands.get(command_name)


def run_init_data(command: IJarvisCommand) -> Dict[str, Any]:
    """
    Run init_data on a command.

    Args:
        command: The command instance

    Returns:
        The result dictionary from init_data()
    """
    return command.init_data()


def main(args: list[str] | None = None) -> int:
    """
    Main entry point for the init_data CLI.

    Args:
        args: Command line arguments (defaults to sys.argv[1:])

    Returns:
        Exit code (0 for success, 1 for error)
    """
    parser = argparse.ArgumentParser(
        description="Initialize data for a specific command"
    )
    parser.add_argument(
        "--command",
        required=True,
        help="Command name to initialize (e.g., 'play_music')"
    )
    parsed = parser.parse_args(args)

    print(f"Looking for command '{parsed.command}'...")

    command = find_command(parsed.command)
    if command is None:
        print(f"Error: Command '{parsed.command}' not found")
        print("\nAvailable commands:")
        for name in sorted(get_all_commands().keys()):
            print(f"  - {name}")
        return 1

    print(f"Found command: {command.command_name}")
    print(f"Description: {command.description}")
    print()
    print("Running init_data()...")

    result = run_init_data(command)

    print(f"Result: {result}")

    if result.get("status") == "no_init_required":
        print(f"\nCommand '{parsed.command}' has no initialization required.")
    elif result.get("status") == "success":
        print(f"\nInitialization completed successfully.")
    elif result.get("status") == "error":
        print(f"\nInitialization failed: {result.get('message', 'Unknown error')}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
