#!/usr/bin/env python3
"""Docker entrypoint — routes to setup mode or normal text mode.

Checks for valid credentials in the config file. If missing or
placeholder values, launches the setup web UI so the user can
register the node. Otherwise, launches text_mode for normal operation.
"""

import json
import os
import shutil
import sys

_PLACEHOLDERS = {"", "your-node-id", "your_api_key_here"}

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/config/config.json")
TEMPLATE_PATH = os.path.join(os.path.dirname(__file__), "..", "config-template.json")


def _seed_config() -> None:
    """Copy config-template.json into place if no config exists."""
    if os.path.exists(CONFIG_PATH):
        return
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if os.path.exists(TEMPLATE_PATH):
        shutil.copy2(TEMPLATE_PATH, CONFIG_PATH)
        print(f"[entrypoint] seeded config from template → {CONFIG_PATH}", flush=True)
    else:
        # Write a minimal config so setup_mode can read/write it
        with open(CONFIG_PATH, "w") as f:
            json.dump({"node_id": "", "api_key": ""}, f, indent=2)
        print(f"[entrypoint] created minimal config → {CONFIG_PATH}", flush=True)


def _has_credentials() -> bool:
    """Return True if config has real (non-placeholder) credentials."""
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
        node_id = config.get("node_id", "")
        api_key = config.get("api_key", "")
        return node_id not in _PLACEHOLDERS and api_key not in _PLACEHOLDERS
    except (FileNotFoundError, json.JSONDecodeError):
        return False


def main() -> None:
    _seed_config()

    if _has_credentials():
        print("[entrypoint] credentials found → starting text mode", flush=True)
        from scripts.text_mode import main as text_main
        text_main()
    else:
        print("[entrypoint] no credentials → starting setup mode", flush=True)
        from scripts.setup_mode import main as setup_main
        setup_main()


if __name__ == "__main__":
    # Ensure project root is on sys.path
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    main()
