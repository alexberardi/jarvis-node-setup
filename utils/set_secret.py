#!/usr/bin/env python3
"""CLI tool for managing secrets in the jarvis-node-setup database."""

import argparse
import sys
from pathlib import Path

# Add project root to path so this script can be run from anywhere
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from services import secret_service

def main():
    parser = argparse.ArgumentParser(description="Manage secrets via CLI.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # set command
    set_parser = subparsers.add_parser("set", help="Set or update a secret.")
    set_parser.add_argument("--key", required=True, help="Secret key")
    set_parser.add_argument("--value", required=True, help="Secret value")
    set_parser.add_argument("--scope", default="integration", help="Secret scope (default: integration)")
    set_parser.add_argument("--type", default="string", help="Secret value type (default: string)")

    # get command
    get_parser = subparsers.add_parser("get", help="Get a secret value.")
    get_parser.add_argument("--key", required=True, help="Secret key")
    get_parser.add_argument("--scope", default="integration", help="Secret scope (default: integration)")

    # delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a secret.")
    delete_parser.add_argument("--key", required=True, help="Secret key")
    delete_parser.add_argument("--scope", default="integration", help="Secret scope (default: integration)")

    # list command
    list_parser = subparsers.add_parser("list", help="List all secrets.")
    list_parser.add_argument("--scope", default="integration", help="Secret scope (default: integration)")

    args = parser.parse_args()

    if args.command == "set":
        secret_service.set_secret(args.key, args.value, args.scope, args.type)
        print(f"Secret '{args.key}' set in scope '{args.scope}' with type '{args.type}'.")
    elif args.command == "get":
        value = secret_service.get_secret_value(args.key, args.scope)
        if value is not None:
            print(value)
        else:
            print(f"Secret '{args.key}' not found in scope '{args.scope}'.")
    elif args.command == "delete":
        secret_service.delete_secret(args.key, args.scope)
        print(f"Secret '{args.key}' deleted from scope '{args.scope}'.")
    elif args.command == "list":
        secrets = secret_service.get_all_secrets(args.scope)
        if not secrets:
            print(f"No secrets found in scope '{args.scope}'.")
        else:
            for secret in secrets:
                print(f"{secret.key} (type: {secret.value_type})")

if __name__ == "__main__":
    main()