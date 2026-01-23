#!/usr/bin/env python3
"""
Sync date keys from jarvis-llm-proxy-api and generate constants file.

Usage:
    python scripts/sync_date_keys.py
"""
import os
from pathlib import Path

import requests

LLM_PROXY_URL = os.getenv("JARVIS_LLM_PROXY_URL", "http://localhost:8000").rstrip("/")
OUTPUT_FILE = Path(__file__).parent.parent / "constants" / "relative_date_keys.py"


def fetch_date_keys() -> dict:
    """Fetch supported date keys from the LLM proxy API."""
    response = requests.get(f"{LLM_PROXY_URL}/v1/adapters/date-keys", timeout=10)
    response.raise_for_status()
    return response.json()


def generate_constants_file(data: dict) -> str:
    """Generate Python constants file from API response."""
    keys = data.get("keys", [])
    version = data.get("version", "unknown")

    lines = [
        '"""',
        "Auto-generated from jarvis-llm-proxy-api /v1/adapters/date-keys",
        f"Version: {version}",
        "",
        "DO NOT EDIT MANUALLY - Run scripts/sync_date_keys.py to update",
        '"""',
        "",
        "",
        "class RelativeDateKeys:",
        '    """Standardized date key constants for adapter training data."""',
        "",
    ]

    for key in sorted(keys):
        const_name = key.upper()
        lines.append(f'    {const_name} = "{key}"')

    lines.append("")
    lines.append("")
    lines.append("# List of all keys for iteration")
    lines.append("ALL_DATE_KEYS = [")
    for key in sorted(keys):
        lines.append(f"    RelativeDateKeys.{key.upper()},")
    lines.append("]")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    print(f"Fetching date keys from {LLM_PROXY_URL}...")
    data = fetch_date_keys()
    print(f"Found {len(data.get('keys', []))} keys")

    content = generate_constants_file(data)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(content)

    print(f"Generated {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
