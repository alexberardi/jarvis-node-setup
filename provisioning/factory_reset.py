"""
Factory reset logic for clearing all provisioning state.

Called from the provisioning API when a user wants to start fresh
(e.g., after moving the node to a new location).
"""

import json
import os
import shutil
from pathlib import Path

from jarvis_log_client import JarvisLogger

from provisioning.startup import clear_provisioned
from provisioning.wifi_credentials import clear_wifi_credentials
from utils.encryption_utils import clear_k2, get_secret_dir

logger = JarvisLogger(service="jarvis-node")

_PROJECT_DIR = Path(__file__).resolve().parent.parent

_CUSTOM_DIRS: list[str] = [
    "commands/custom_commands",
    "agents/custom_agents",
    "device_families/custom_families",
    "device_managers/custom_managers",
    "routines/custom_routines",
]


def factory_reset() -> dict:
    """Clear all provisioning state for a fresh start.

    Removes:
    - .provisioned marker
    - K2 encryption key + metadata
    - WiFi credentials
    - Node database + DB encryption key
    - Config credentials (node_id, api_key reset to placeholders)
    - MQTT broker credentials (mqtt_username/mqtt_password cleared so a fresh
      reprovision re-fetches them from command-center instead of reusing a stale
      password from the previous install)
    - All Pantry-installed packages (custom commands, agents, protocols, managers, routines)
    - Pantry package metadata (~/.jarvis/packages/)

    Does NOT remove:
    - K1 master key (Fernet key — reused for encrypting new secrets)
    - config.json structure (URLs will be overwritten during re-provisioning)

    Returns:
        Dict with 'cleared' list of what was removed.
    """
    cleared: list[str] = []

    # 1. Clear provisioning marker
    try:
        clear_provisioned()
        cleared.append("provisioning_marker")
    except Exception as e:
        logger.warning("Failed to clear provisioning marker", error=str(e))

    # 2. Clear K2 encryption key
    try:
        clear_k2()
        cleared.append("k2_key")
    except Exception as e:
        logger.warning("Failed to clear K2 key", error=str(e))

    # 3. Clear WiFi credentials
    try:
        clear_wifi_credentials()
        cleared.append("wifi_credentials")
    except Exception as e:
        logger.warning("Failed to clear WiFi credentials", error=str(e))

    # 4. Remove node database + DB encryption key
    db_path = Path(os.getenv("JARVIS_NODE_DB", "./jarvis_node.db"))
    if db_path.exists():
        try:
            db_path.unlink()
            cleared.append("node_database")
        except Exception as e:
            logger.warning("Failed to remove node database", error=str(e))

    secret_dir = get_secret_dir()
    db_key_file = secret_dir / "db.key"
    if db_key_file.exists():
        try:
            db_key_file.unlink()
            cleared.append("db_key")
        except Exception as e:
            logger.warning("Failed to remove DB key", error=str(e))

    # 5. Reset node credentials in config.json to placeholders
    config_path = os.environ.get("CONFIG_PATH")
    if config_path:
        try:
            config: dict = {}
            with open(config_path) as f:
                config = json.load(f)

            config["node_id"] = "your-node-id"
            config["api_key"] = "your_api_key_here"
            # Clear the shared MQTT broker credential too. It's bound to the
            # install this node was paired with; a fresh reprovision (possibly to
            # a different install whose broker password differs) must NOT keep the
            # old one. The runtime fetch only refreshes creds when they're ABSENT
            # (mqtt_tts_listener: ``if not username or not password``), so a stale
            # value here makes the node skip the refresh and connect with the
            # wrong password (broker rc=5, "not authorised") forever.
            config["mqtt_username"] = ""
            config["mqtt_password"] = ""

            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)

            cleared.append("config_credentials")
            cleared.append("mqtt_credentials")
        except Exception as e:
            logger.warning("Failed to reset config credentials", error=str(e))

    # 6. Remove all Pantry-installed packages
    cleared.extend(_clear_pantry_packages())

    logger.info("Factory reset complete", cleared=cleared)
    return {"cleared": cleared}


def _clear_pantry_packages() -> list[str]:
    """Remove all Pantry-installed content from custom_* directories and metadata.

    Returns:
        List of cleared item labels.
    """
    cleared: list[str] = []

    for rel_dir in _CUSTOM_DIRS:
        custom_dir = _PROJECT_DIR / rel_dir
        if not custom_dir.is_dir():
            continue
        for entry in custom_dir.iterdir():
            if entry.name.startswith(("_", ".")) or not entry.is_dir():
                continue
            try:
                shutil.rmtree(entry)
                cleared.append(f"pantry:{rel_dir}/{entry.name}")
                logger.info("Removed Pantry component", path=str(entry))
            except Exception as e:
                logger.warning("Failed to remove Pantry component", path=str(entry), error=str(e))

    # Remove package metadata and shared libs
    packages_dir = Path.home() / ".jarvis" / "packages"
    if packages_dir.is_dir():
        try:
            shutil.rmtree(packages_dir)
            cleared.append("pantry:packages_metadata")
            logger.info("Removed Pantry package metadata")
        except Exception as e:
            logger.warning("Failed to remove Pantry metadata", error=str(e))

    return cleared
