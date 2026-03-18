"""Repository for command registry (enabled/disabled state per command)."""

from typing import Dict

from sqlalchemy.orm import Session

from models.command_registry import CommandRegistry


class CommandRegistryRepository:
    """CRUD operations for the command_registry table."""

    def __init__(self, db: Session):
        self.db = db

    def get_all(self) -> Dict[str, bool]:
        """Get all registered commands with their enabled state.

        Returns:
            Dict mapping command_name to enabled (True/False).
        """
        rows = self.db.query(CommandRegistry).all()
        return {row.command_name: bool(row.enabled) for row in rows}

    def set_enabled(self, command_name: str, enabled: bool) -> None:
        """Upsert the enabled state for a command.

        Args:
            command_name: The command to update.
            enabled: Whether the command should be enabled.
        """
        row = self.db.query(CommandRegistry).filter_by(command_name=command_name).first()
        if row:
            row.enabled = 1 if enabled else 0
        else:
            row = CommandRegistry(command_name=command_name, enabled=1 if enabled else 0)
            self.db.add(row)
        self.db.commit()

    def ensure_registered(self, command_names: list[str]) -> None:
        """Insert any missing commands with enabled=1 (default).

        Existing entries are not modified.

        Args:
            command_names: List of command names to register.
        """
        existing = {
            row.command_name
            for row in self.db.query(CommandRegistry.command_name).all()
        }
        for name in command_names:
            if name not in existing:
                self.db.add(CommandRegistry(command_name=name, enabled=1))
        self.db.commit()
