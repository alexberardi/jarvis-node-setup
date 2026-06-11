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
from repositories.agent_registry_repository import AgentRegistryRepository
from repositories.command_registry_repository import CommandRegistryRepository
from repositories.disabled_fast_path_repository import DisabledFastPathRepository
from services.secret_service import get_secret_value
from utils.agent_discovery_service import get_agent_discovery_service
from utils.audio_volume import get_volume_percent
from utils.command_discovery_service import get_command_discovery_service
from utils.config_service import Config
from utils.device_family_discovery_service import get_device_family_discovery_service
from utils.device_manager_discovery_service import get_device_manager_discovery_service
from utils.encryption_utils import get_k2
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

SCHEMA_VERSION: int = 1
COMMANDS_SCHEMA_VERSION: int = 2


class _EntryGuard:
    """Isolates third-party property/method failures while building a snapshot entry.

    Pantry-installed commands, agents, device families, and device managers can
    raise from any property access (a stale `scope="node"` JarvisSecret took the
    whole snapshot down on 2026-05-26). `_EntryGuard.get()` swallows the failure,
    logs a warning with the component name, and records a tag in `self.errors`
    so the snapshot entry can surface a "configuration error" badge in the
    mobile UI without dropping the whole entry.
    """

    def __init__(self, log_ctx: dict[str, Any]):
        self.errors: list[str] = []
        self._log_ctx = log_ctx

    def get(self, fn, default, tag: str):
        try:
            return fn()
        except Exception as e:
            logger.warning(
                f"Snapshot field failed: {tag}", **self._log_ctx, error=str(e)
            )
            self.errors.append(tag)
            return default


def _b64url_encode(data: bytes) -> str:
    """Base64url encode without padding."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _build_secrets_list(
    secrets,
    *,
    include_values: bool,
    user_id: int | None,
    log_ctx: dict[str, Any],
    errors: list[str],
    honor_include_values: bool = True,
) -> list[dict[str, Any]]:
    """Build a snapshot `secrets` array from a component's required_secrets.

    Each secret is processed in isolation; a single misbehaved JarvisSecret
    only loses its own entry rather than killing the whole list. The
    `honor_include_values=False` mode preserves the pre-existing device-manager
    behavior of never returning sensitive values, even when the caller asked
    for include_values=True.
    """
    result: list[dict[str, Any]] = []
    for secret in secrets:
        try:
            secret_user_id = user_id if secret.scope == "user" else None
            value: str | None = get_secret_value(
                secret.key, secret.scope, user_id=secret_user_id
            )
            entry: dict[str, Any] = {
                "key": secret.key,
                "scope": secret.scope,
                "description": secret.description,
                "value_type": secret.value_type,
                "required": secret.required,
                "is_sensitive": secret.is_sensitive,
                "is_set": bool(value),
            }
            if secret.friendly_name:
                entry["friendly_name"] = secret.friendly_name
            if getattr(secret, "enum_values", None):
                entry["enum_values"] = secret.enum_values
            if getattr(secret, "presets", None):
                entry["presets"] = secret.presets
            if value:
                if honor_include_values:
                    if not secret.is_sensitive or include_values:
                        entry["value"] = value
                else:
                    if not secret.is_sensitive:
                        entry["value"] = value
            result.append(entry)
        except Exception as e:
            key = getattr(secret, "key", "<unknown>")
            logger.warning(
                "Skipping bad secret entry", **log_ctx, secret=key, error=str(e)
            )
            errors.append(f"secret:{key}")
    return result


def build_snapshot(include_values: bool = False, user_id: int | None = None) -> dict[str, Any]:
    """Build a plain (unencrypted) settings snapshot from all discovered commands.

    Args:
        include_values: If True, include actual values for ALL secrets (including
            sensitive ones like API keys). Used for secret sync between nodes.
            The snapshot is still encrypted with K2 in transit.
        user_id: If provided, include user-scoped secret values for this user.
    """
    # Set user context so commands that read user-scoped secrets in their
    # required_secrets property (e.g. email's get_email_provider()) work correctly.
    from jarvis_command_sdk.context import set_current_user_id
    set_current_user_id(user_id)

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

    # Load disabled fast-path pattern IDs in one query so we can mark each
    # pattern's enabled state in the snapshot without a per-command DB hit.
    disabled_fp_by_cmd: dict[str, set[str]] = {}
    try:
        db = SessionLocal()
        try:
            disabled_fp_by_cmd = DisabledFastPathRepository(db).get_all_disabled()
        finally:
            db.close()
    except Exception as e:
        logger.warning("Failed to read disabled fast paths", error=str(e))

    command_entries: list[dict[str, Any]] = []
    for cmd in commands.values():
        # command_name is the entry's primary key — if it can't be read, we
        # can't render the row at all, so skip and log.
        try:
            cmd_name: str = cmd.command_name
        except Exception as e:
            logger.warning("Skipping command with unreadable command_name", error=str(e))
            continue

        try:
            g = _EntryGuard({"command": cmd_name})

            secrets_list = _build_secrets_list(
                g.get(lambda: cmd.required_secrets, [], "required_secrets"),
                include_values=include_values,
                user_id=user_id,
                log_ctx={"command": cmd_name},
                errors=g.errors,
            )

            cmd_entry: dict[str, Any] = {
                "command_name": cmd_name,
                "description": g.get(lambda: cmd.description, "", "description"),
                "secrets": secrets_list,
                "enabled": registry.get(cmd_name, True),
            }

            associated = g.get(lambda: cmd.associated_service, None, "associated_service")
            if associated:
                cmd_entry["associated_service"] = associated

            setup_guide = g.get(lambda: getattr(cmd, "setup_guide", None), None, "setup_guide")
            if setup_guide:
                cmd_entry["setup_guide"] = setup_guide

            auth_obj = g.get(lambda: cmd.authentication, None, "authentication")
            if auth_obj:
                auth_dict = g.get(lambda: auth_obj.to_dict(), None, "authentication")
                if auth_dict:
                    cmd_entry["authentication"] = auth_dict

            params = g.get(lambda: cmd.parameters, [], "parameters")
            if params:
                serialized = g.get(
                    lambda: [p.to_dict() for p in params], None, "parameters"
                )
                if serialized:
                    cmd_entry["parameters"] = serialized

            # Fast-path patterns: per-pattern metadata + enabled state from the
            # sparse disabled-list table. Mobile renders these in the "Inspect
            # fast-paths" screen for granular per-pattern opt-out when two
            # commands' patterns collide.
            fp_patterns = g.get(
                lambda: getattr(cmd, "fast_path_patterns", []) or [],
                [],
                "fast_path_patterns",
            )
            if fp_patterns:
                fp_serialized = g.get(
                    lambda: [
                        {
                            "id": p.id,
                            "description": p.description,
                            "example": p.example,
                            "enabled": p.id
                            not in disabled_fp_by_cmd.get(cmd_name, set()),
                        }
                        for p in fp_patterns
                    ],
                    None,
                    "fast_path_patterns",
                )
                if fp_serialized:
                    cmd_entry["fast_paths"] = fp_serialized

            if g.errors:
                cmd_entry["_errors"] = g.errors
            command_entries.append(cmd_entry)
        except Exception as e:
            # Last-resort catch: emit a stub entry so the mobile UI can still
            # render the command name with a configuration-error badge instead
            # of the command silently vanishing from the list.
            logger.warning(
                "Command entry build failed entirely",
                command=cmd_name,
                error=str(e),
            )
            command_entries.append(
                {
                    "command_name": cmd_name,
                    "description": "",
                    "secrets": [],
                    "enabled": registry.get(cmd_name, True),
                    "_errors": ["entry_build_failed"],
                }
            )

    # Synthetic entries for command modules that failed to import — discovery
    # only logs these, so without a stub row the command silently vanishes
    # from the mobile list (resilient-installs incident #3 class). Reuses the
    # `_errors` badge plumbing end to end: mobile renders "Failed to load".
    try:
        failed_command_modules: dict[str, str] = dict(service.get_failed_modules())
    except Exception as e:
        logger.debug("Could not read failed command modules", error=str(e))
        failed_command_modules = {}
    for module_name, import_error in failed_command_modules.items():
        command_entries.append(
            {
                "command_name": module_name,
                "description": "",
                "secrets": [],
                "enabled": False,
                "_errors": [f"import_failed: {str(import_error)[:200]}"],
            }
        )

    # Build device family entries (per-family isolation so one bad protocol
    # doesn't prevent all families from appearing in the snapshot)
    family_entries: list[dict[str, Any]] = []
    try:
        family_service = get_device_family_discovery_service()
        families = family_service.get_all_families_for_snapshot()

        for family in families.values():
            try:
                family_name: str = family.protocol_name
            except Exception as e:
                logger.warning("Skipping family with unreadable protocol_name", error=str(e))
                continue

            try:
                g = _EntryGuard({"family": family_name})

                secrets_list_f = _build_secrets_list(
                    g.get(lambda: family.required_secrets, [], "required_secrets"),
                    include_values=include_values,
                    user_id=None,
                    log_ctx={"family": family_name},
                    errors=g.errors,
                )

                actions = g.get(lambda: family.supported_actions, [], "supported_actions")
                actions_serialized = g.get(
                    lambda: [a.to_dict() for a in actions], [], "supported_actions"
                )

                family_entry: dict[str, Any] = {
                    "family_name": family_name,
                    "friendly_name": g.get(lambda: family.friendly_name, "", "friendly_name"),
                    "description": g.get(lambda: family.description, "", "description"),
                    "connection_type": g.get(lambda: family.connection_type, "", "connection_type"),
                    "supported_domains": g.get(lambda: family.supported_domains, [], "supported_domains"),
                    "supported_actions": actions_serialized,
                    "secrets": secrets_list_f,
                    "is_configured": g.get(
                        lambda: (
                            len(family.validate_secrets()) == 0
                            if hasattr(family, "validate_secrets")
                            else True
                        ),
                        True,
                        "validate_secrets",
                    ),
                    "is_custom": "device_families.custom_families"
                    in (type(family).__module__ or ""),
                }
                auth_obj = g.get(lambda: family.authentication, None, "authentication")
                if auth_obj:
                    auth_dict = g.get(lambda: auth_obj.to_dict(), None, "authentication")
                    if auth_dict:
                        family_entry["authentication"] = auth_dict
                setup_guide = g.get(
                    lambda: getattr(family, "setup_guide", None), None, "setup_guide"
                )
                if setup_guide:
                    family_entry["setup_guide"] = setup_guide

                if g.errors:
                    family_entry["_errors"] = g.errors
                family_entries.append(family_entry)
            except Exception as e:
                logger.warning(
                    "Device family entry build failed entirely",
                    family=family_name,
                    error=str(e),
                )
                family_entries.append(
                    {
                        "family_name": family_name,
                        "friendly_name": family_name,
                        "description": "",
                        "connection_type": "",
                        "supported_domains": [],
                        "supported_actions": [],
                        "secrets": [],
                        "is_configured": False,
                        "is_custom": False,
                        "_errors": ["entry_build_failed"],
                    }
                )
    except Exception as e:
        logger.warning("Failed to discover device families", error=str(e))

    # Build device manager entries (per-manager isolation)
    manager_entries: list[dict[str, Any]] = []
    try:
        manager_service = get_device_manager_discovery_service()
        managers = manager_service.get_all_managers_for_snapshot()

        for mgr in managers.values():
            try:
                manager_name: str = mgr.name
            except Exception as e:
                logger.warning("Skipping manager with unreadable name", error=str(e))
                continue

            try:
                g = _EntryGuard({"manager": manager_name})

                secrets_list_m = _build_secrets_list(
                    g.get(lambda: mgr.required_secrets, [], "required_secrets"),
                    include_values=include_values,
                    user_id=None,
                    log_ctx={"manager": manager_name},
                    errors=g.errors,
                    honor_include_values=False,  # preserves pre-existing manager behavior
                )

                mgr_entry: dict[str, Any] = {
                    "manager_name": manager_name,
                    "friendly_name": g.get(lambda: mgr.friendly_name, "", "friendly_name"),
                    "description": g.get(lambda: mgr.description, "", "description"),
                    "can_edit_devices": g.get(
                        lambda: mgr.can_edit_devices, False, "can_edit_devices"
                    ),
                    "is_available": g.get(lambda: mgr.is_available(), False, "is_available"),
                    "secrets": secrets_list_m,
                }
                auth_obj = g.get(lambda: mgr.authentication, None, "authentication")
                if auth_obj:
                    auth_dict = g.get(lambda: auth_obj.to_dict(), None, "authentication")
                    if auth_dict:
                        mgr_entry["authentication"] = auth_dict

                if g.errors:
                    mgr_entry["_errors"] = g.errors
                manager_entries.append(mgr_entry)
            except Exception as e:
                logger.warning(
                    "Device manager entry build failed entirely",
                    manager=manager_name,
                    error=str(e),
                )
                manager_entries.append(
                    {
                        "manager_name": manager_name,
                        "friendly_name": manager_name,
                        "description": "",
                        "can_edit_devices": False,
                        "is_available": False,
                        "secrets": [],
                        "_errors": ["entry_build_failed"],
                    }
                )
    except Exception as e:
        logger.warning("Failed to discover device managers", error=str(e))

    # Agent entries — surfaced so the mobile Agents tab can list and toggle
    # background agents. Agents not yet in agent_registry are treated as
    # enabled (matches AgentRegistryRepository.is_enabled fallback).
    agent_entries: list[dict[str, Any]] = []
    try:
        agent_service = get_agent_discovery_service()
        agents = agent_service.get_all_agents()

        try:
            db = SessionLocal()
            try:
                agent_registry = AgentRegistryRepository(db).get_all()
            finally:
                db.close()
        except Exception as e:
            logger.warning("Failed to read agent registry", error=str(e))
            agent_registry = {}

        # In-memory map of agents the scheduler tripped after 3 failures.
        # Imported lazily so the snapshot service stays usable in tests that
        # don't bootstrap the scheduler singleton.
        auto_disabled_reasons: dict[str, str] = {}
        try:
            from services.agent_scheduler_service import AgentSchedulerService
            auto_disabled_reasons = AgentSchedulerService().get_auto_disabled_reasons()
        except Exception as e:
            logger.debug("Could not read auto-disabled reasons from scheduler", error=str(e))

        agent_to_service = _build_agent_to_service_map()

        for agent in agents.values():
            try:
                agent_name: str = agent.name
            except Exception as e:
                logger.warning("Skipping agent with unreadable name", error=str(e))
                continue

            try:
                g = _EntryGuard({"agent": agent_name})

                schedule_obj = g.get(lambda: agent.schedule, None, "schedule")
                schedule_dict = {
                    "interval_seconds": g.get(
                        lambda: schedule_obj.interval_seconds, 0, "schedule.interval_seconds"
                    ) if schedule_obj else 0,
                    "run_on_startup": g.get(
                        lambda: schedule_obj.run_on_startup, False, "schedule.run_on_startup"
                    ) if schedule_obj else False,
                }

                secrets_list_a = _build_secrets_list(
                    g.get(lambda: agent.required_secrets, [], "required_secrets"),
                    include_values=include_values,
                    user_id=None,
                    log_ctx={"agent": agent_name},
                    errors=g.errors,
                )

                entry_a: dict[str, Any] = {
                    "agent_name": agent_name,
                    "description": g.get(lambda: agent.description, "", "description"),
                    "enabled": agent_registry.get(agent_name, True),
                    "schedule": schedule_dict,
                    "secrets": secrets_list_a,
                }
                service_label = agent_to_service.get(agent_name)
                if service_label:
                    entry_a["associated_service"] = service_label

                auto_reason = auto_disabled_reasons.get(agent_name)
                if auto_reason:
                    entry_a["auto_disabled_reason"] = auto_reason

                if g.errors:
                    entry_a["_errors"] = g.errors
                agent_entries.append(entry_a)
            except Exception as e:
                logger.warning(
                    "Agent entry build failed entirely",
                    agent=agent_name,
                    error=str(e),
                )
                agent_entries.append(
                    {
                        "agent_name": agent_name,
                        "description": "",
                        "enabled": agent_registry.get(agent_name, True),
                        "schedule": {"interval_seconds": 0, "run_on_startup": False},
                        "secrets": [],
                        "_errors": ["entry_build_failed"],
                    }
                )

        # Unconfigured agents (missing required secrets) are LISTED with a
        # "needs setup" marker instead of hidden — incident #3 of the
        # resilient-installs PRD was email_alerts dead for ~2 months with no
        # UI trace. The secrets list is built normally so the user can FIX
        # the agent from the card; the scheduler still never sees these
        # (get_all_agents stays configured-only).
        try:
            unconfigured_agents = dict(agent_service.get_unconfigured_agents())
        except Exception as e:
            logger.debug("Could not read unconfigured agents", error=str(e))
            unconfigured_agents = {}

        for agent_name, (agent, missing_secrets) in unconfigured_agents.items():
            try:
                g = _EntryGuard({"agent": agent_name})

                schedule_obj = g.get(lambda: agent.schedule, None, "schedule")
                schedule_dict = {
                    "interval_seconds": g.get(
                        lambda: schedule_obj.interval_seconds, 0, "schedule.interval_seconds"
                    ) if schedule_obj else 0,
                    "run_on_startup": g.get(
                        lambda: schedule_obj.run_on_startup, False, "schedule.run_on_startup"
                    ) if schedule_obj else False,
                }

                secrets_list_u = _build_secrets_list(
                    g.get(lambda: agent.required_secrets, [], "required_secrets"),
                    include_values=include_values,
                    user_id=None,
                    log_ctx={"agent": agent_name},
                    errors=g.errors,
                )

                entry_u: dict[str, Any] = {
                    "agent_name": agent_name,
                    "description": g.get(lambda: agent.description, "", "description"),
                    "enabled": agent_registry.get(agent_name, True),
                    "schedule": schedule_dict,
                    "secrets": secrets_list_u,
                    "unconfigured": True,
                    "missing_secrets": list(missing_secrets),
                }
                service_label = agent_to_service.get(agent_name)
                if service_label:
                    entry_u["associated_service"] = service_label

                if g.errors:
                    entry_u["_errors"] = g.errors
                agent_entries.append(entry_u)
            except Exception as e:
                logger.warning(
                    "Unconfigured agent entry build failed entirely",
                    agent=agent_name,
                    error=str(e),
                )
                agent_entries.append(
                    {
                        "agent_name": agent_name,
                        "description": "",
                        "enabled": agent_registry.get(agent_name, True),
                        "schedule": {"interval_seconds": 0, "run_on_startup": False},
                        "secrets": [],
                        "unconfigured": True,
                        "missing_secrets": list(missing_secrets),
                        "_errors": ["entry_build_failed"],
                    }
                )

        # Synthetic entries for agent modules that failed to import — same
        # `_errors`-badge plumbing as the command section above.
        try:
            failed_agent_modules: dict[str, str] = dict(agent_service.get_failed_modules())
        except Exception as e:
            logger.debug("Could not read failed agent modules", error=str(e))
            failed_agent_modules = {}
        for module_name, import_error in failed_agent_modules.items():
            agent_entries.append(
                {
                    "agent_name": module_name,
                    "description": "",
                    "enabled": False,
                    "schedule": {"interval_seconds": 0, "run_on_startup": False},
                    "secrets": [],
                    "_errors": [f"import_failed: {str(import_error)[:200]}"],
                }
            )
    except Exception as e:
        logger.warning("Failed to discover agents", error=str(e))

    set_current_user_id(None)  # reset context

    # Read PA volume once (subprocess call) — fallback to 100 when unreadable.
    # Returning the live PA value keeps the mobile slider honest on a fresh
    # node where config.json has no persisted volume_percent yet.
    _live_volume = get_volume_percent()

    node_config: dict[str, Any] = {
        "wake_word_threshold": Config.get_float("wake_word_threshold", 0.5),
        "silence_threshold": Config.get_int("silence_threshold", 5000),
        "silence_duration": Config.get_float("silence_duration", 0.5),
        "min_record_seconds": Config.get_float("min_record_seconds", 1.0),
        "max_record_seconds": Config.get_int("max_record_seconds", 7),
        "barge_in_enabled": Config.get_bool("barge_in_enabled", True),
        "wake_ack_audio_enabled": Config.get_bool("wake_ack_audio_enabled", True),
        "follow_up_listen_seconds": Config.get_int("follow_up_listen_seconds", 4),
        "follow_up_silence_duration": Config.get_float("follow_up_silence_duration", 0.5),
        "follow_up_min_record_after_onset_secs": Config.get_float(
            "follow_up_min_record_after_onset_secs", 0.7
        ),
        "follow_up_min_speech_secs": Config.get_float("follow_up_min_speech_secs", 0.3),
        # Live PA reading via audio_volume.get_volume_percent — was
        # ``Config.get_int("volume_percent", 100)``, which lied on a fresh
        # node: slider showed 100 while PA's actual sink was at the
        # system default, so the displayed value only became real after
        # the user moved the slider and tapped Save.
        "volume_percent": _live_volume if _live_volume is not None else 100,
        "led_enabled": Config.get_bool("led_enabled", True),
        "led_brightness_percent": Config.get_int("led_brightness_percent", 100),
        # Daily-restart hygiene knob. The Pi Zero 2W accumulates ~5-50 MB/h of
        # allocator + library-cache growth that no in-process GC reaches; a
        # short restart at a quiet hour clears it. See
        # services/maintenance_restart_service.py for the scheduler.
        "maintenance_restart_enabled": Config.get_bool(
            "maintenance_restart_enabled", True,
        ),
        "maintenance_restart_at_time": Config.get_str(
            "maintenance_restart_at_time", "03:00",
        ),
        "maintenance_restart_rss_ceiling_mb": Config.get_int(
            "maintenance_restart_rss_ceiling_mb", 320,
        ),
    }

    # Detected hardware — surfaces ReSpeaker HAT presence + audio backend to
    # the mobile app so it can hide/show HAT-only controls and warn when the
    # node's hardware doesn't support a feature the user is configuring.
    hardware: dict[str, Any] = {}
    try:
        from services.respeaker_led_service import respeaker_available
        hardware["hat_detected"] = respeaker_available()
        hardware["led_chain_available"] = respeaker_available()
    except Exception:
        hardware["hat_detected"] = False
        hardware["led_chain_available"] = False

    try:
        from utils.audio_volume import get_audio_card, is_muted
        hardware["audio_card"] = get_audio_card()
        muted = is_muted()
        if muted is not None:
            hardware["is_muted"] = muted
    except Exception:
        hardware["audio_card"] = None

    # gpiozero is only available on Pi-class hardware; treat any import or
    # detection error as "no button" rather than failing the whole snapshot.
    try:
        import gpiozero  # noqa: F401
        hardware["button_available"] = hardware.get("hat_detected", False)
    except Exception:
        hardware["button_available"] = False

    node_config["hardware"] = hardware

    return {
        "schema_version": SCHEMA_VERSION,
        "commands_schema_version": COMMANDS_SCHEMA_VERSION,
        "commands": command_entries,
        "agents": agent_entries,
        "device_families": family_entries,
        "device_managers": manager_entries,
        "node_config": node_config,
    }


def _build_agent_to_service_map() -> dict[str, str]:
    """Map agent_name -> package display_name by walking ~/.jarvis/packages/*.json.

    Used to label agents in the mobile snapshot with their parent integration
    (e.g., "ha_snapshot" -> "Home Assistant"). Agents that don't come from a
    Pantry package (e.g., built-in agents) are omitted from the map.
    """
    from pathlib import Path

    packages_dir = Path.home() / ".jarvis" / "packages"
    if not packages_dir.exists():
        return {}

    mapping: dict[str, str] = {}
    for meta_path in packages_dir.glob("*.json"):
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            display_name = meta.get("display_name") or meta.get("package_name")
            if not display_name:
                continue
            for component in meta.get("components", []):
                if component.get("type") == "agent":
                    agent_name = component.get("name")
                    if agent_name:
                        mapping[agent_name] = display_name
        except Exception as e:
            logger.debug("Skipping unreadable package metadata", path=str(meta_path), error=str(e))
    return mapping


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


def handle_snapshot_request(request_id: str, include_values: bool = False, user_id: int | None = None) -> bool:
    """Full flow: confirm request, build snapshot, encrypt, upload.

    Args:
        request_id: The settings request ID from CC.
        include_values: If True, include sensitive secret values in the snapshot
            (for secret sync between nodes).
        user_id: If provided, include user-scoped secret values for this user.

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
    snapshot: dict[str, Any] = build_snapshot(include_values=include_values, user_id=user_id)
    logger.info(
        "Snapshot built",
        request_id=request_id[:8],
        command_count=len(snapshot["commands"]),
        agent_count=len(snapshot.get("agents", [])),
        family_count=len(snapshot.get("device_families", [])),
        manager_count=len(snapshot.get("device_managers", [])),
        node_config_keys=len(snapshot.get("node_config", {})),
    )

    # Encrypt
    try:
        encrypted: dict[str, str] = encrypt_snapshot(snapshot, node_id)
    except ValueError as e:
        logger.error("Snapshot encryption failed", request_id=request_id[:8], error=str(e))
        return False

    # Upload
    return upload_snapshot(node_id, request_id, encrypted)
