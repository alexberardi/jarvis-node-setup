#!/usr/bin/env python3
"""Docker entrypoint — routes to setup, text, or voice mode.

Checks for valid credentials in the config file. If missing or
placeholder values, launches the setup web UI so the user can
register the node. Otherwise launches:

  * voice mode (scripts.main) — the full audio runtime (mic capture,
    wake word, speaker) — when JARVIS_NODE_MODE=voice. Requires the
    audio image (Dockerfile.audio) and host audio passed in.
  * text mode (scripts.text_mode) — the headless REST node, no audio —
    otherwise (the default).

Note: JARVIS_NODE_MODE is the *node run mode* and is distinct from the
``voice_mode`` config key, which is the command-center response style
(brief/text).
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


def _apply_config_service_env() -> None:
    """Expose the registered config-service URL to jarvis-config-client.

    setup_mode saves the config-service URL the user entered to config.json
    (jarvis_config_service_url). jarvis-config-client reads JARVIS_CONFIG_URL
    from the environment, and only rewrites localhost-registered services to a
    reachable host when JARVIS_CONFIG_URL_STYLE=remote. So when the config
    service is on another machine (the container case), set both — otherwise
    the runtime resolves command-center/MQTT/etc. as 'localhost' and can't
    reach them. An explicit JARVIS_CONFIG_URL already in the env wins.
    """
    if os.environ.get("JARVIS_CONFIG_URL"):
        return
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return
    url = (cfg.get("jarvis_config_service_url") or "").strip()
    if not url:
        return
    os.environ["JARVIS_CONFIG_URL"] = url
    from urllib.parse import urlparse
    host = urlparse(url).hostname or ""
    # Choose the URL style from the vantage the config host reveals:
    #   host.docker.internal → this node runs in Docker on the same host as the
    #     stack (a Docker peer) → 'dockerized' (localhost → host.docker.internal).
    #   a real IP/hostname    → this node is off-box (a Pi on the LAN, or a node
    #     on another host) → 'external': uses each service's published coords,
    #     rewriting BOTH the localhost-registered broker AND container-name HTTP
    #     rows to the server host. ('remote' only fixes the localhost rows, so it
    #     leaves container-name services unreachable off-box — that's why it was
    #     only half-working.)
    if host == "host.docker.internal":
        os.environ.setdefault("JARVIS_CONFIG_URL_STYLE", "dockerized")
    elif host and host not in ("localhost", "127.0.0.1"):
        os.environ.setdefault("JARVIS_CONFIG_URL_STYLE", "external")
    print(
        f"[entrypoint] config service: {url} "
        f"(style={os.environ.get('JARVIS_CONFIG_URL_STYLE', 'default')})",
        flush=True,
    )


def main() -> None:
    _seed_config()

    if not _has_credentials():
        print("[entrypoint] no credentials → starting setup mode", flush=True)
        from scripts.setup_mode import main as setup_main
        setup_main()
        return

    _apply_config_service_env()
    mode = os.environ.get("JARVIS_NODE_MODE", "text").strip().lower()
    if mode == "voice":
        print("[entrypoint] credentials + voice mode → starting voice runtime", flush=True)
        from scripts.main import main as voice_main
        voice_main()
    else:
        print("[entrypoint] credentials found → starting text mode", flush=True)
        from scripts.text_mode import main as text_main
        text_main()


if __name__ == "__main__":
    # Ensure project root is on sys.path
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    main()
