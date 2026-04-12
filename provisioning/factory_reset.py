"""
Factory reset logic for clearing all provisioning state.

Called from the provisioning API when a user wants to start fresh
(e.g., after moving the node to a new location).
"""

import json
import os
from pathlib import Path

from jarvis_log_client import JarvisLogger

from provisioning.startup import clear_provisioned
from provisioning.wifi_credentials import clear_wifi_credentials
from utils.encryption_utils import clear_k2, get_secret_dir

logger = JarvisLogger(service="jarvis-node")


def factory_reset() -> dict:
    """Clear all provisioning state for a fresh start.

    Removes:
    - .provisioned marker
    - K2 encryption key + metadata
    - WiFi credentials
    - Node database + DB encryption key
    - Config credentials (node_id, api_key reset to placeholders)

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

            with open(config_path, "w") as f:
                json.dump(config, f, indent=2)

            cleared.append("config_credentials")
        except Exception as e:
            logger.warning("Failed to reset config credentials", error=str(e))

    logger.info("Factory reset complete", cleared=cleared)
    return {"cleared": cleared}
