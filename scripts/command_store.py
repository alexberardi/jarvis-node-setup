#!/usr/bin/env python3
"""Command Store CLI — install, remove, list, search, and manage custom commands.

Usage:
    python scripts/command_store.py install --url <github_url> [--version <tag>]
    python scripts/command_store.py install <command_name> [--version <tag>]
    python scripts/command_store.py remove <command_name>
    python scripts/command_store.py list
    python scripts/command_store.py info <command_name>
    python scripts/command_store.py search <query>
    python scripts/command_store.py update [command_name | --all]
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from services.command_store_service import (  # noqa: E402
    install_from_github,
    remove,
    list_installed,
    get_installed_metadata,
    InstallError,
    RemoveError,
)


def _get_store_client():
    """Create a CommandStoreClient, or None if store is not configured."""
    try:
        from clients.command_store_client import CommandStoreClient
        from utils.config_service import Config

        store_url = Config.get_str("command_store_url", "")
        if not store_url:
            return None

        jwt_token = Config.get_str("command_store_jwt", "")
        household_id = Config.get_str("household_id", "")
        return CommandStoreClient(
            store_url=store_url,
            jwt_token=jwt_token or None,
            household_id=household_id or None,
        )
    except Exception:
        return None


def cmd_install(args: argparse.Namespace) -> None:
    """Install a command from a GitHub URL or the store."""
    if args.url:
        # Direct GitHub install
        try:
            manifest = install_from_github(
                repo_url=args.url,
                version_tag=args.version,
                skip_tests=args.skip_tests,
            )
            print(f"Installed: {manifest.name} v{manifest.version}")
            if manifest.secrets:
                print(f"  Secrets to configure: {', '.join(s.key for s in manifest.secrets)}")
                print("  Use scripts/install_command.py to set secret values.")
        except InstallError as e:
            print(f"Install failed: {e}", file=sys.stderr)
            sys.exit(1)
    elif args.command_name:
        # Install from store
        client = _get_store_client()
        if not client:
            print("Error: Command store not configured. Use --url for direct GitHub install.", file=sys.stderr)
            sys.exit(1)

        try:
            info = client.get_download_info(args.command_name, args.version)
            repo_url = info["github_repo_url"]
            tag = info.get("git_tag")
            print(f"Downloading {args.command_name} from store...")
            manifest = install_from_github(
                repo_url=repo_url,
                version_tag=tag,
                skip_tests=args.skip_tests,
            )
            client.report_install(args.command_name)
            print(f"Installed: {manifest.name} v{manifest.version}")
            if manifest.secrets:
                print(f"  Secrets to configure: {', '.join(s.key for s in manifest.secrets)}")
        except Exception as e:
            print(f"Install failed: {e}", file=sys.stderr)
            sys.exit(1)
        finally:
            client.close()
    else:
        print("Error: provide a command name or --url", file=sys.stderr)
        sys.exit(1)


def cmd_remove(args: argparse.Namespace) -> None:
    """Remove an installed custom command."""
    try:
        remove(args.command_name)
        print(f"Removed: {args.command_name}")
    except RemoveError as e:
        print(f"Remove failed: {e}", file=sys.stderr)
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    """List installed custom commands."""
    installed = list_installed()
    if not installed:
        print("No custom commands installed.")
        return

    print(f"{'Command':<30} {'Version':<12} {'Source'}")
    print("-" * 70)
    for meta in installed:
        name = meta.get("command_name", "?")
        version = meta.get("version", "?")
        source = meta.get("repo_url", "local") or "local"
        if source.startswith("https://github.com/"):
            source = source.replace("https://github.com/", "gh:")
        print(f"{name:<30} {version:<12} {source}")


def cmd_info(args: argparse.Namespace) -> None:
    """Show details for an installed or store command."""
    # Check local first
    meta = get_installed_metadata(args.command_name)
    if meta:
        print(f"Command:      {meta.get('command_name')}")
        print(f"Display Name: {meta.get('display_name', 'N/A')}")
        print(f"Version:      {meta.get('version', '?')}")
        print(f"Author:       {meta.get('author', 'N/A')}")
        print(f"Repo:         {meta.get('repo_url', 'N/A')}")
        print(f"Installed:    {meta.get('installed_at', 'N/A')}")
        print(f"Verified:     {meta.get('verified', False)}")
        danger = meta.get("danger_rating")
        if danger is not None:
            print(f"Danger:       {danger}/5")
        return

    # Try store
    client = _get_store_client()
    if client:
        try:
            data = client.get_command(args.command_name)
            print(f"Command:      {data.get('command_name')}")
            print(f"Display Name: {data.get('display_name', 'N/A')}")
            print(f"Description:  {data.get('description', 'N/A')}")
            print(f"Version:      {data.get('latest_version', '?')}")
            print(f"Author:       {data.get('author', {}).get('github', 'N/A')}")
            print(f"Installs:     {data.get('install_count', 0)}")
            print(f"Verified:     {data.get('verified', False)}")
            print(f"Danger:       {data.get('danger_rating', '?')}/5")
            print(f"Categories:   {', '.join(data.get('categories', []))}")
            return
        except Exception:
            pass
        finally:
            client.close()

    print(f"Command '{args.command_name}' not found locally or in the store.", file=sys.stderr)
    sys.exit(1)


def cmd_search(args: argparse.Namespace) -> None:
    """Search the command store catalog."""
    client = _get_store_client()
    if not client:
        print("Error: Command store not configured.", file=sys.stderr)
        sys.exit(1)

    try:
        result = client.search(query=args.query, category=args.category)
        commands = result.get("commands", [])

        if not commands:
            print("No commands found.")
            return

        print(f"{'Command':<25} {'Version':<10} {'Installs':<10} {'Danger':<8} {'Verified'}")
        print("-" * 75)
        for cmd in commands:
            name = cmd.get("command_name", "?")
            ver = cmd.get("latest_version", "?")
            installs = cmd.get("install_count", 0)
            danger = cmd.get("danger_rating", "?")
            verified = "yes" if cmd.get("verified") else ""
            print(f"{name:<25} {ver:<10} {installs:<10} {danger:<8} {verified}")

        total = result.get("total", len(commands))
        if total > len(commands):
            print(f"\nShowing {len(commands)} of {total}. Use --page for more.")
    except Exception as e:
        print(f"Search failed: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        client.close()


def cmd_update(args: argparse.Namespace) -> None:
    """Update installed commands to latest versions."""
    installed = list_installed()
    if not installed:
        print("No custom commands installed.")
        return

    client = _get_store_client()

    if args.command_name and not args.all:
        # Update single command
        targets = [m for m in installed if m.get("command_name") == args.command_name]
        if not targets:
            print(f"Command '{args.command_name}' is not installed.")
            sys.exit(1)
    else:
        targets = installed

    updated = 0
    for meta in targets:
        name = meta.get("command_name", "")
        current_ver = meta.get("version", "")
        repo_url = meta.get("repo_url")

        if not repo_url:
            print(f"  {name}: no repo URL, skipping")
            continue

        # Check for newer version
        latest_ver = None
        if client:
            try:
                info = client.get_download_info(name)
                latest_ver = info.get("version")
            except Exception:
                pass

        if latest_ver and latest_ver != current_ver:
            print(f"  {name}: {current_ver} -> {latest_ver}")
            try:
                install_from_github(repo_url, version_tag=f"v{latest_ver}", skip_tests=True)
                if client:
                    client.report_install(name)
                updated += 1
            except InstallError as e:
                print(f"    Update failed: {e}")
        else:
            print(f"  {name}: up to date ({current_ver})")

    if client:
        client.close()

    print(f"\n{updated} command(s) updated.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Jarvis Command Store CLI"
    )
    subparsers = parser.add_subparsers(dest="action", required=True)

    # install
    install_parser = subparsers.add_parser("install", help="Install a command")
    install_parser.add_argument("command_name", nargs="?", help="Command name (from store)")
    install_parser.add_argument("--url", help="GitHub repo URL (direct install)")
    install_parser.add_argument("--version", help="Git tag to install (e.g. v1.0.0)")
    install_parser.add_argument("--skip-tests", action="store_true", help="Skip container tests")
    install_parser.set_defaults(func=cmd_install)

    # remove
    remove_parser = subparsers.add_parser("remove", help="Remove a custom command")
    remove_parser.add_argument("command_name", help="Command to remove")
    remove_parser.set_defaults(func=cmd_remove)

    # list
    list_parser = subparsers.add_parser("list", help="List installed custom commands")
    list_parser.set_defaults(func=cmd_list)

    # info
    info_parser = subparsers.add_parser("info", help="Show command details")
    info_parser.add_argument("command_name", help="Command to inspect")
    info_parser.set_defaults(func=cmd_info)

    # search
    search_parser = subparsers.add_parser("search", help="Search the command store")
    search_parser.add_argument("query", nargs="?", help="Search query")
    search_parser.add_argument("--category", help="Filter by category")
    search_parser.set_defaults(func=cmd_search)

    # update
    update_parser = subparsers.add_parser("update", help="Update commands to latest versions")
    update_parser.add_argument("command_name", nargs="?", help="Command to update (or --all)")
    update_parser.add_argument("--all", action="store_true", help="Update all commands")
    update_parser.set_defaults(func=cmd_update)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
