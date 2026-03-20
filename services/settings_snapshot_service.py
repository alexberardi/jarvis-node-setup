"""Settings snapshot service -- collect command settings, encrypt, upload.

When the mobile app requests a settings snapshot, the node:
1. Enumerates all commands and their required_secrets
2. Checks which secrets are set (metadata only, no values)
3. Builds a snapshot JSON
4. Encrypts with K2 (AES-256-GCM)
5. Uploads to CC for mobile to poll
"""

import base64
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from db import SessionLocal
from repositories.command_registry_repository import CommandRegistryRepository
from services.secret_service import get_secret_value
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config
from utils.device_family_discovery_service import get_device_family_discovery_service
from utils.device_manager_discovery_service import get_device_manager_discovery_service
from utils.encryption_utils import get_k2
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

SCHEMA_VERSION: int = 1
COMMANDS_SCHEMA_VERSION: int = 2


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_snapshot(include_values: bool = False) -> dict[str, Any]:
    """Build a plain (unencrypted) settings snapshot from all discovered commands.

    Args:
        include_values: If True, include actual values for ALL secrets (including
            sensitive ones like API keys). Used for secret sync between nodes.
            The snapshot is still encrypted with K2 in transit.
    """
    service = get_command_discovery_service()
    service.refresh_now()
    commands = service.get_all_commands(include_disabled=True)

    # Get enabled states from registry
    try:
        db = SessionLocal()
        try:
            repo = CommandRegistryRepository(db)
            registry = repo.get_all()
        finally:
            db.close()
    except Exception as e:
        logger.warning("Failed to read command registry", error=str(e))
        registry = {}

    command_entries: list[dict[str, Any]] = []
    for cmd in commands.values():
        secrets_list: list[dict[str, Any]] = []
        for secret in cmd.required_secrets:
            value: str | None = get_secret_value(secret.key, secret.scope)
            entry: dict[str, Any] = {
                "key": secret.key,
                "scope": secret.scope,
                "description": secret.description,
                "value_type": secret.value_type,
                "required": secret.required,
                "is_sensitive": secret.is_sensitive,
                "is_set": bool(value),  # empty string = not set
            }
            if secret.friendly_name:
                entry["friendly_name"] = secret.friendly_name
            # Include value: always for non-sensitive, only with include_values for sensitive
            if value and (not secret.is_sensitive or include_values):
                entry["value"] = value
            secrets_list.append(entry)

        cmd_entry: dict[str, Any] = {
            "command_name": cmd.command_name,
            "description": cmd.description,
            "secrets": secrets_list,
            "enabled": registry.get(cmd.command_name, True),
        }
        if cmd.associated_service:
            cmd_entry["associated_service"] = cmd.associated_service
        if hasattr(cmd, "setup_guide") and cmd.setup_guide:
            cmd_entry["setup_guide"] = cmd.setup_guide
        if cmd.authentication:
            cmd_entry["authentication"] = cmd.authentication.to_dict()
        params = cmd.parameters
        if params:
            cmd_entry["parameters"] = [p.to_dict() for p in params]
        command_entries.append(cmd_entry)

    # Build device family entries
    family_entries: list[dict[str, Any]] = []
    try:
        family_service = get_device_family_discovery_service()
        families = family_service.get_all_families_for_snapshot()

        for family in families.values():
            secrets_list_f: list[dict[str, Any]] = []
            for secret in family.required_secrets:
                value_f: str | None = get_secret_value(secret.key, secret.scope)
                entry_f: dict[str, Any] = {
                    "key": secret.key,
                    "scope": secret.scope,
                    "description": secret.description,
                    "value_type": secret.value_type,
                    "required": secret.required,
                    "is_sensitive": secret.is_sensitive,
                    "is_set": bool(value_f),
                }
                if secret.friendly_name:
                    entry_f["friendly_name"] = secret.friendly_name
                if not secret.is_sensitive and value_f:
                    entry_f["value"] = value_f
                secrets_list_f.append(entry_f)

            family_entry: dict[str, Any] = {
                "family_name": family.protocol_name,
                "friendly_name": family.friendly_name,
                "description": family.description,
                "connection_type": family.connection_type,
                "supported_domains": family.supported_domains,
                "supported_actions": [a.to_dict() for a in family.supported_actions],
                "secrets": secrets_list_f,
                "is_configured": len(family.validate_secrets()) == 0,
            }
            if family.authentication:
                family_entry["authentication"] = family.authentication.to_dict()
            family_entries.append(family_entry)
    except Exception as e:
        logger.warning("Failed to build device family entries", error=str(e))

    # Build device manager entries
    manager_entries: list[dict[str, Any]] = []
    try:
        manager_service = get_device_manager_discovery_service()
        managers = manager_service.get_all_managers_for_snapshot()

        for mgr in managers.values():
            secrets_list_m: list[dict[str, Any]] = []
            for secret in mgr.required_secrets:
                value_m: str | None = get_secret_value(secret.key, secret.scope)
                entry_m: dict[str, Any] = {
                    "key": secret.key,
                    "scope": secret.scope,
                    "description": secret.description,
                    "value_type": secret.value_type,
                    "required": secret.required,
                    "is_sensitive": secret.is_sensitive,
                    "is_set": bool(value_m),
                }
                if secret.friendly_name:
                    entry_m["friendly_name"] = secret.friendly_name
                if not secret.is_sensitive and value_m:
                    entry_m["value"] = value_m
                secrets_list_m.append(entry_m)

            mgr_entry: dict[str, Any] = {
                "manager_name": mgr.name,
                "friendly_name": mgr.friendly_name,
                "description": mgr.description,
                "can_edit_devices": mgr.can_edit_devices,
                "is_available": mgr.is_available(),
                "secrets": secrets_list_m,
            }
            if mgr.authentication:
                mgr_entry["authentication"] = mgr.authentication.to_dict()
            manager_entries.append(mgr_entry)
    except Exception as e:
        logger.warning("Failed to build device manager entries", error=str(e))

    return {
        "schema_version": SCHEMA_VERSION,
        "commands_schema_version": COMMANDS_SCHEMA_VERSION,
        "commands": command_entries,
        "device_families": family_entries,
        "device_managers": manager_entries,
    }


def encrypt_snapshot(snapshot: dict[str, Any], node_id: str) -> dict[str, str]:
    """Encrypt snapshot with K2 using AES-256-GCM.

    Returns dict with ciphertext, nonce, tag (all base64url encoded).
    Mirrors the encryption format used by configPushService on mobile.
    """
    k2_data = get_k2()
    if k2_data is None:
        raise ValueError("K2 key not available -- node may not be provisioned")

    plaintext_json: str = json.dumps(snapshot, separators=(",", ":"))

    # Generate 12-byte nonce
    nonce: bytes = os.urandom(12)

    # AAD binds ciphertext to the node and config type
    config_type: str = "settings:snapshot"
    aad: bytes = f"{node_id}:{config_type}".encode("utf-8")

    # Encrypt raw JSON bytes (mobile's native crypto bridge base64url-encodes the result)
    aesgcm = AESGCM(k2_data.k2)
    ct_with_tag: bytes = aesgcm.encrypt(nonce, plaintext_json.encode("utf-8"), aad)

    # AESGCM.encrypt returns ciphertext || tag (last 16 bytes are tag)
    ciphertext: bytes = ct_with_tag[:-16]
    tag: bytes = ct_with_tag[-16:]

    return {
        "ciphertext": _b64url_encode(ciphertext),
        "nonce": _b64url_encode(nonce),
        "tag": _b64url_encode(tag),
        "aad_schema_version": SCHEMA_VERSION,
        "aad_commands_schema_version": COMMANDS_SCHEMA_VERSION,
        "aad_revision": 1,
    }


def upload_snapshot(
    node_id: str,
    request_id: str,
    encrypted: dict[str, str],
) -> bool:
    """Upload encrypted snapshot to CC."""
    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.error("Cannot upload snapshot: command center URL not resolved")
        return False

    url = f"{base_url.rstrip('/')}/api/v0/nodes/{node_id}/settings/requests/{request_id}/snapshot"
    result = RestClient.put(url, data=encrypted, timeout=15)
    if result is None:
        logger.error("Snapshot upload failed", request_id=request_id[:8])
        return False

    logger.info("Snapshot uploaded", request_id=request_id[:8])
    return True


def handle_snapshot_request(request_id: str, include_values: bool = False) -> bool:
    """Full flow: confirm request, build snapshot, encrypt, upload.

    Args:
        request_id: The settings request ID from CC.
        include_values: If True, include sensitive secret values in the snapshot
            (for secret sync between nodes).

    Returns True if successful.
    """
    node_id: str = Config.get_str("node_id", "") or ""
    if not node_id:
        logger.error("Cannot handle snapshot request: node_id not configured")
        return False

    base_url: str = get_command_center_url() or ""
    if not base_url:
        logger.error("Cannot handle snapshot request: command center URL not resolved")
        return False

    # Confirm the request with CC
    confirm_url = f"{base_url.rstrip('/')}/api/v0/nodes/{node_id}/settings/requests/{request_id}"
    confirm_result = RestClient.get(confirm_url, timeout=10)
    if confirm_result is None:
        logger.error("Snapshot request confirmation failed", request_id=request_id[:8])
        return False

    logger.info("Snapshot request confirmed", request_id=request_id[:8])

    # Build snapshot
    snapshot: dict[str, Any] = build_snapshot(include_values=include_values)
    logger.info(
        "Snapshot built",
        request_id=request_id[:8],
        command_count=len(snapshot["commands"]),
    )

    # Encrypt
    try:
        encrypted: dict[str, str] = encrypt_snapshot(snapshot, node_id)
    except ValueError as e:
        logger.error("Snapshot encryption failed", request_id=request_id[:8], error=str(e))
        return False

    # Upload
    return upload_snapshot(node_id, request_id, encrypted)
