"""Routine sync — pull the household routine set from command-center.

Routines are owned server-side (per household) in command-center. The node is a
pull-on-nudge cache: on boot and whenever CC publishes a `routines/sync` nudge,
we GET the current household set and rewrite the DB layer of the routine store
(the top precedence layer of commands.routine_command._load_routines).

We only touch the DB layer. Hardcoded defaults and Pantry custom_routines files
are never modified, and a DB routine is pruned only if the server no longer has
it AND it is not a default/custom slug. CC is authoritative for the DB layer; the
defaults < custom_routines < DB precedence is preserved.
"""

from typing import Any, Dict

from jarvis_log_client import JarvisLogger

from clients.rest_client import RestClient
from db import SessionLocal
from repositories.command_data_repository import CommandDataRepository
from utils.config_service import Config
from utils.service_discovery import get_command_center_url

logger = JarvisLogger(service="jarvis-node")

_COMMAND_NAME = "routine"


def _protected_slugs() -> set[str]:
    """Slugs that must never be pruned: hardcoded defaults + Pantry custom files."""
    try:
        from commands.routine_command import RoutineCommand, _load_custom_routine_files

        return set(RoutineCommand._default_routines().keys()) | set(
            _load_custom_routine_files().keys()
        )
    except Exception as exc:  # never let an enumeration error widen the prune
        logger.warning("Could not enumerate protected routine slugs", error=str(exc))
        # Fail safe: protect nothing extra is risky, so signal "protect everything"
        # by returning a sentinel the caller treats as "skip prune".
        raise


def pull_routines() -> int:
    """Pull the household routine set and rewrite the DB layer.

    Returns the number of server routines applied. Fail-soft: on an unreachable
    or erroring CC, the local store is left untouched and 0 is returned.
    """
    node_id = Config.get_str("node_id", "") or ""
    if not node_id:
        logger.warning("Routine pull skipped — no node_id configured")
        return 0

    cc_url = get_command_center_url()
    if not cc_url:
        logger.warning("Routine pull skipped — no command-center URL")
        return 0

    url = f"{cc_url.rstrip('/')}/api/v0/nodes/{node_id}/routines"
    resp = RestClient.get(url)
    if resp is None:
        # CC unreachable — keep whatever we already have (do NOT prune).
        logger.warning("Routine pull failed — CC unreachable, keeping local routines")
        return 0

    server: Dict[str, Dict[str, Any]] = resp.get("routines", {}) or {}

    # Compute protected slugs before mutating; if enumeration fails, skip the
    # prune entirely (still apply server routines) rather than risk deletion.
    try:
        protected = _protected_slugs()
        do_prune = True
    except Exception:
        protected = set()
        do_prune = False

    db = SessionLocal()
    try:
        repo = CommandDataRepository(db)

        for slug, definition in server.items():
            repo.save(_COMMAND_NAME, slug, definition)

        if do_prune:
            existing = {
                row.get("_data_key")
                for row in repo.get_all(_COMMAND_NAME)
                if row.get("_data_key")
            }
            for slug in existing:
                if slug not in server and slug not in protected:
                    repo.delete(_COMMAND_NAME, slug)
                    logger.info("Pruned server-removed routine", slug=slug)
    finally:
        db.close()

    logger.info("Routines pulled from CC", count=len(server))
    return len(server)
