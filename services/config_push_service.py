"""Config push handler — polls CC, decrypts with K2, dispatches, ACKs.

Mobile pushes encrypted config to CC, CC stores it and notifies via MQTT.
This service handles the node side: poll → decrypt → dispatch → ACK.
"""

import base64
import json
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from db import SessionLocal
from repositories.command_registry_repository import CommandRegistryRepository
from utils.config_service import Config
from utils.encryption_utils import get_k2
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")


def process_pending_configs() -> int:
    """Poll CC for pending configs, decrypt, dispatch, ACK.

    Returns:
        Number of configs successfully processed.
    """
    pending = _fetch_pending()
    if not pending:
        return 0

    processed: int = 0
    # Node knows its own ID; CC pending endpoint doesn't return it
    node_id: str = Config.get_str("node_id", "") or ""

    for item in pending:
        # CC returns "id", not "push_id"
        push_id: str = item.get("id", "") or item.get("push_id", "")
        config_type: str = item.get("config_type", "")

        try:
            config_data = _decrypt_config(
                ciphertext_b64=item.get("ciphertext", ""),
                nonce_b64=item.get("nonce", ""),
                tag_b64=item.get("tag", ""),
                node_id=node_id,
                config_type=config_type,
            )
            _dispatch_config(config_type, config_data)
            _ack_config(push_id)
            processed += 1
            logger.info(
                "Config push processed",
                push_id=push_id[:8],
                config_type=config_type,
            )
        except Exception as e:
            logger.error(
                "Failed to process config push",
                push_id=push_id[:8],
                config_type=config_type,
                error=str(e),
            )

    return processed


def _fetch_pending() -> list[dict[str, Any]]:
    """GET /api/v0/nodes/{node_id}/config/pending via RestClient."""
    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.error("Cannot fetch pending configs: command center URL not resolved")
        return []

    node_id: str = Config.get_str("node_id", "") or ""
    if not node_id:
        logger.error("Cannot fetch pending configs: node_id not configured")
        return []

    url = f"{base_url.rstrip('/')}/api/v0/nodes/{node_id}/config/pending"
    result = RestClient.get(url, timeout=15)

    if result is None:
        return []

    # CC returns {"pending": [...]} or a list directly
    if isinstance(result, dict):
        return result.get("pending", [])
    if isinstance(result, list):
        return result
    return []


def _decrypt_config(
    ciphertext_b64: str,
    nonce_b64: str,
    tag_b64: str,
    node_id: str,
    config_type: str,
) -> dict[str, str]:
    """AES-256-GCM decrypt with K2.

    Mobile encrypts: base64url(JSON.stringify(configData)) → AES-256-GCM → ciphertext + tag.
    So after GCM decrypt we get base64url-encoded JSON, which we decode then parse.

    Args:
        ciphertext_b64: Base64url-encoded ciphertext (may lack padding).
        nonce_b64: Base64url-encoded 12-byte nonce (may lack padding).
        tag_b64: Base64url-encoded 16-byte GCM tag (may lack padding).
        node_id: Node ID for AAD.
        config_type: Config type for AAD (e.g., "auth:home_assistant").

    Returns:
        Decrypted config data as dict.

    Raises:
        ValueError: If K2 is not available or decryption fails.
    """
    k2_data = get_k2()
    if k2_data is None:
        raise ValueError("K2 key not available — node may not be provisioned")

    ciphertext = _b64url_decode(ciphertext_b64)
    nonce = _b64url_decode(nonce_b64)
    tag = _b64url_decode(tag_b64)

    # AES-GCM: ciphertext || tag is the standard input for AESGCM.decrypt
    aad = f"{node_id}:{config_type}".encode("utf-8")
    aesgcm = AESGCM(k2_data.k2)

    plaintext_bytes = aesgcm.decrypt(nonce, ciphertext + tag, aad)

    # Native crypto module base64url-decodes inputs before encrypting,
    # so decrypted plaintext is raw JSON bytes
    return json.loads(plaintext_bytes)


def _dispatch_config(config_type: str, config_data: dict[str, str]) -> None:
    """Route decrypted config to the appropriate handler.

    - auth:* types → find command with matching authentication.provider,
      call store_auth_values().
    - command_registry → update enabled/disabled state for a command.
    - Other types → store each key-value pair as a secret.
    """
    if config_type.startswith("auth:"):
        provider = config_type[len("auth:"):]  # e.g., "home_assistant"
        _dispatch_auth(provider, config_data)
    elif config_type == "command_registry":
        _dispatch_command_registry(config_data)
    else:
        _dispatch_secrets(config_data)


def _dispatch_command_registry(config_data: dict[str, str]) -> None:
    """Update command enabled/disabled state in the registry."""
    command_name = config_data.get("command_name", "")
    enabled_str = config_data.get("enabled", "true")
    enabled = enabled_str.lower() in ("true", "1", "yes")

    if not command_name:
        logger.warning("command_registry push missing command_name")
        return

    db = SessionLocal()
    try:
        repo = CommandRegistryRepository(db)
        repo.set_enabled(command_name, enabled)
    finally:
        db.close()

    logger.info("Command registry updated", command=command_name, enabled=enabled)

    # Refresh discovery cache so the change takes effect immediately
    from utils.command_discovery_service import get_command_discovery_service
    get_command_discovery_service().refresh_now()


def _dispatch_auth(provider: str, config_data: dict[str, str]) -> None:
    """Find command matching auth provider and call store_auth_values()."""
    from utils.command_discovery_service import get_command_discovery_service

    service = get_command_discovery_service()
    commands = service.get_all_commands()

    # Find first command that declares this provider
    for cmd in commands.values():
        if cmd.authentication and cmd.authentication.provider == provider:
            logger.info(
                "Dispatching auth config to command",
                provider=provider,
                command=cmd.command_name,
            )
            cmd.store_auth_values(config_data)
            return

    # No command matched — check device families
    from utils.device_family_discovery_service import get_device_family_discovery_service

    family_service = get_device_family_discovery_service()
    families = family_service.get_all_families_for_snapshot()

    for family in families.values():
        if family.authentication and family.authentication.provider == provider:
            logger.info(
                "Dispatching auth config to device family",
                provider=provider,
                family=family.protocol_name,
            )
            family.store_auth_values(config_data)
            return

    logger.warning("No command or device family found for auth provider", provider=provider)


def _dispatch_secrets(config_data: dict[str, str]) -> None:
    """Store non-auth config values as secrets.

    If __user_id__ is present, secrets are stored with scope='user' and the
    given user_id. Otherwise, scope is looked up from existing DB rows
    (pre-seeded by install_command), falling back to 'integration'.
    """
    from services.secret_service import get_secret_scope, set_secret

    user_id_str = config_data.pop("__user_id__", None)
    user_id: int | None = int(user_id_str) if user_id_str else None

    for key, value in config_data.items():
        if user_id is not None:
            # Mobile sent a user_id — this is a personal secret
            set_secret(key, str(value), "user", user_id=user_id)
            logger.debug("Stored user secret", key=key, user_id=user_id)
        else:
            scope: str = get_secret_scope(key) or "integration"
            set_secret(key, str(value), scope)
            logger.debug("Stored config secret", key=key, scope=scope)


def _ack_config(push_id: str) -> None:
    """POST /api/v0/nodes/{node_id}/config/{push_id}/ack."""
    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.error("Cannot ACK config push: command center URL not resolved")
        return

    node_id: str = Config.get_str("node_id", "") or ""
    if not node_id:
        logger.error("Cannot ACK config push: node_id not configured")
        return

    url = f"{base_url.rstrip('/')}/api/v0/nodes/{node_id}/config/{push_id}/ack"
    result = RestClient.post(url, data={}, timeout=10)

    if result is None:
        logger.warning("Config push ACK failed", push_id=push_id[:8])


def _b64url_decode(data: str) -> bytes:
    """Decode base64url with missing padding tolerance."""
    padding_needed = (4 - len(data) % 4) % 4
    return base64.urlsafe_b64decode(data + "=" * padding_needed)
