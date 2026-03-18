#!/usr/bin/env python3
"""Install a command by seeding its required secrets into the local DB.

Discovers the command class, reads its required_secrets, and upserts each
into the secrets table with an empty default value. Existing values are
never overwritten.

Usage:
    python scripts/install_command.py get_weather
    python scripts/install_command.py --all
    python scripts/install_command.py --list
    python scripts/install_command.py --all --dry-run-deps   # resolve only
    python scripts/install_command.py --all --skip-deps       # skip resolution
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db import SessionLocal
from repositories.command_registry_repository import CommandRegistryRepository
from services.secret_service import seed_command_secrets
from utils.command_discovery_service import CommandDiscoveryService
from utils.dependency_resolver import resolve_all, ResolutionResult
from utils.device_family_discovery_service import DeviceFamilyDiscoveryService

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
CUSTOM_REQUIREMENTS_PATH = os.path.join(REPO_ROOT, "custom-requirements.txt")
DEPENDENCY_SNAPSHOT_PATH = os.path.join(REPO_ROOT, "dependency-snapshot.json")


def _run_db_migrations() -> None:
    """Run Alembic migrations to ensure DB schema is up to date."""
    try:
        from alembic.config import Config as AlembicConfig
        from alembic import command as alembic_command

        alembic_cfg = AlembicConfig(os.path.join(REPO_ROOT, "alembic.ini"))
        alembic_command.upgrade(alembic_cfg, "head")
        print("Database migrations: OK")
    except Exception as e:
        print(f"Database migration failed: {e}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Install a command by seeding its secrets into the DB"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("command_name", nargs="?", help="Command to install (e.g. get_weather)")
    group.add_argument("--all", action="store_true", help="Install all discovered commands")
    group.add_argument("--list", action="store_true", help="List all commands and their secrets")
    parser.add_argument("--skip-deps", action="store_true", help="Skip dependency resolution")
    parser.add_argument("--dry-run-deps", action="store_true", help="Resolve deps only, don't install")
    args = parser.parse_args()

    # Ensure DB exists and schema is current
    _run_db_migrations()

    # Discover commands (one-shot, no background thread)
    discovery = CommandDiscoveryService(refresh_interval=99999)
    discovery.refresh_now()
    all_commands = discovery.get_all_commands()

    if not all_commands:
        print("No commands discovered. Check the commands/ directory.")
        sys.exit(1)

    if args.list:
        _list_commands(all_commands)
        return

    # Dependency resolution (unless --skip-deps)
    if not args.skip_deps:
        base_req = _find_base_requirements()
        result = _resolve_deps(base_req, all_commands, dry_run=args.dry_run_deps)
        if not result.success:
            sys.exit(1)
        if args.dry_run_deps:
            return

    if args.all:
        _install_all(all_commands)
        return

    # Single command install
    cmd = all_commands.get(args.command_name)
    if not cmd:
        print(f"Command '{args.command_name}' not found.")
        print(f"Available: {', '.join(sorted(all_commands.keys()))}")
        sys.exit(1)

    _install_command(cmd)
    _ensure_registered([cmd.command_name])


def _find_base_requirements() -> str:
    """Return path to the base requirements file for this platform."""
    pi_req = os.path.join(REPO_ROOT, "requirements-pi.txt")
    base_req = os.path.join(REPO_ROOT, "requirements.txt")
    if os.path.exists(pi_req) and os.path.exists("/sys/firmware/devicetree/base/model"):
        return pi_req
    return base_req


def _resolve_deps(
    base_req_path: str,
    commands: dict,
    dry_run: bool = False,
) -> ResolutionResult:
    """Resolve dependencies and write output files. Returns the result."""
    print("\nResolving command dependencies...")
    result = resolve_all(base_req_path, commands)

    if not result.success:
        print("\nDependency conflicts detected:")
        for conflict in result.conflicts:
            print(f"\n  {conflict.package_name}:")
            for source, spec in conflict.sources:
                print(f"    {source}: {spec or '(any)'}")
            print(f"    {conflict.reason}")
        print("\nFix conflicts before installing. Use --skip-deps to bypass.")
        return result

    if not result.merged_specs:
        print("No additional command dependencies to install.")
        return result

    print(f"Resolved {len(result.merged_specs)} command dependencies:")
    for spec in result.merged_specs:
        print(f"  {spec}")

    if dry_run:
        print("\n(dry run — nothing installed)")
        return result

    # Write custom-requirements.txt
    with open(CUSTOM_REQUIREMENTS_PATH, "w") as f:
        f.write("# Auto-generated by install_command.py — do not edit manually\n")
        for spec in result.merged_specs:
            f.write(f"{spec}\n")
    print(f"\nWrote {CUSTOM_REQUIREMENTS_PATH}")

    # Write dependency-snapshot.json
    with open(DEPENDENCY_SNAPSHOT_PATH, "w") as f:
        json.dump(result.snapshot, f, indent=2)
    print(f"Wrote {DEPENDENCY_SNAPSHOT_PATH}")

    # Install
    print("\nInstalling command dependencies...")
    pip_cmd = [sys.executable, "-m", "pip", "install", "-r", CUSTOM_REQUIREMENTS_PATH, "--quiet"]
    ret = subprocess.run(pip_cmd)
    if ret.returncode != 0:
        print("pip install failed — check output above")
        result.success = False
    else:
        print("Command dependencies installed.")

    return result


def _list_commands(commands: dict) -> None:
    print(f"\n{len(commands)} commands discovered:\n")
    for name in sorted(commands.keys()):
        cmd = commands[name]
        secrets = cmd.all_possible_secrets
        if secrets:
            secret_keys = [f"  {s.key} ({s.scope}, {s.value_type}, {'required' if s.required else 'optional'})" for s in secrets]
            print(f"  {name}")
            for sk in secret_keys:
                print(f"    {sk}")
        else:
            print(f"  {name}  (no secrets)")

    # List device families
    family_discovery = DeviceFamilyDiscoveryService()
    families = family_discovery.get_all_families_for_snapshot()
    if families:
        print(f"\n{len(families)} device families discovered:\n")
        for name in sorted(families.keys()):
            family = families[name]
            conn = family.connection_type
            secrets = family.required_secrets
            print(f"  {family.friendly_name} ({conn})")
            if secrets:
                for s in secrets:
                    print(f"    {s.key} ({s.scope}, {s.value_type}, {'required' if s.required else 'optional'})")
            else:
                print(f"    (no secrets - LAN only)")
    print()


def _install_command(cmd) -> None:
    secrets = cmd.all_possible_secrets
    if not secrets:
        print(f"{cmd.command_name}: no secrets to install")
        return

    inserted = seed_command_secrets(secrets)
    total = len(secrets)
    print(f"{cmd.command_name}: {inserted} new / {total} total secrets seeded")

    for s in secrets:
        status = "NEW" if inserted > 0 else "exists"
        print(f"  {s.key} ({s.scope}, {s.value_type}) - {status}")


def _install_all(commands: dict) -> None:
    total_inserted = 0
    total_secrets = 0

    for name in sorted(commands.keys()):
        cmd = commands[name]
        secrets = cmd.all_possible_secrets
        if not secrets:
            continue

        inserted = seed_command_secrets(secrets)
        total_inserted += inserted
        total_secrets += len(secrets)
        print(f"  {name}: {inserted} new / {len(secrets)} total")

    # Seed device family secrets
    family_discovery = DeviceFamilyDiscoveryService()
    families = family_discovery.get_all_families_for_snapshot()
    for name in sorted(families.keys()):
        family = families[name]
        secrets = family.required_secrets
        if not secrets:
            continue

        inserted = seed_command_secrets(secrets)
        total_inserted += inserted
        total_secrets += len(secrets)
        print(f"  [device] {family.friendly_name}: {inserted} new / {len(secrets)} total")

    print(f"\nDone: {total_inserted} new secrets seeded ({total_secrets} total across all commands)")
    _ensure_registered(list(commands.keys()))


def _ensure_registered(command_names: list[str]) -> None:
    """Register commands in the command_registry table."""
    db = SessionLocal()
    try:
        repo = CommandRegistryRepository(db)
        repo.ensure_registered(command_names)
    finally:
        db.close()


if __name__ == "__main__":
    main()
