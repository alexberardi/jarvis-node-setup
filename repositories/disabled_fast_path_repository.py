"""Repository for the disabled_fast_paths table.

Tracks the sparse set of fast-path patterns a user has opted out of from
the mobile inspect UI. Patterns not in the table default to enabled.
"""

from typing import Set

from sqlalchemy.orm import Session

from models.disabled_fast_path import DisabledFastPath


class DisabledFastPathRepository:
    """CRUD operations for the disabled_fast_paths table."""

    def __init__(self, db: Session):
        self.db = db

    def is_disabled(self, command_name: str, pattern_id: str) -> bool:
        """Return True if this specific pattern has been disabled by the user."""
        row = (
            self.db.query(DisabledFastPath)
            .filter_by(command_name=command_name, pattern_id=pattern_id)
            .first()
        )
        return row is not None

    def get_disabled_ids(self, command_name: str) -> Set[str]:
        """Return the set of disabled pattern_ids for a specific command.

        Returns an empty set if nothing is disabled. Callers pass this into
        `IJarvisCommand.pre_route(..., disabled_pattern_ids=...)`.
        """
        rows = (
            self.db.query(DisabledFastPath.pattern_id)
            .filter_by(command_name=command_name)
            .all()
        )
        return {row.pattern_id for row in rows}

    def get_all_disabled(self) -> dict[str, Set[str]]:
        """Return all disabled patterns grouped by command_name.

        Used by the snapshot service to mark `enabled=False` on per-pattern
        entries in a single DB pass.
        """
        rows = self.db.query(DisabledFastPath).all()
        result: dict[str, Set[str]] = {}
        for row in rows:
            result.setdefault(row.command_name, set()).add(row.pattern_id)
        return result

    def set_disabled(self, command_name: str, pattern_id: str, disabled: bool) -> None:
        """Toggle a pattern's disabled state.

        Disabled=True inserts a row (no-op if already present). Disabled=False
        deletes the row. Sparse storage — enabled is the implicit default.
        """
        existing = (
            self.db.query(DisabledFastPath)
            .filter_by(command_name=command_name, pattern_id=pattern_id)
            .first()
        )
        if disabled:
            if existing is None:
                self.db.add(
                    DisabledFastPath(command_name=command_name, pattern_id=pattern_id)
                )
                self.db.commit()
        else:
            if existing is not None:
                self.db.delete(existing)
                self.db.commit()
