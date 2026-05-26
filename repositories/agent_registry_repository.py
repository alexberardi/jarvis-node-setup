"""Repository for agent registry (enabled/disabled state per agent)."""

from typing import Dict

from sqlalchemy.orm import Session

from models.agent_registry import AgentRegistry


class AgentRegistryRepository:
    """CRUD operations for the agent_registry table."""

    def __init__(self, db: Session):
        self.db = db

    def get_all(self) -> Dict[str, bool]:
        """Get all registered agents with their enabled state.

        Returns:
            Dict mapping agent_name to enabled (True/False).
        """
        rows = self.db.query(AgentRegistry).all()
        return {row.agent_name: bool(row.enabled) for row in rows}

    def is_enabled(self, agent_name: str) -> bool:
        """Return True if the agent is enabled (or unknown to the registry).

        Agents not yet registered default to enabled so newly-installed agents
        run on their first scheduled tick without requiring an explicit insert.
        """
        row = self.db.query(AgentRegistry).filter_by(agent_name=agent_name).first()
        if row is None:
            return True
        return bool(row.enabled)

    def set_enabled(self, agent_name: str, enabled: bool) -> None:
        """Upsert the enabled state for an agent."""
        row = self.db.query(AgentRegistry).filter_by(agent_name=agent_name).first()
        if row:
            row.enabled = 1 if enabled else 0
        else:
            row = AgentRegistry(agent_name=agent_name, enabled=1 if enabled else 0)
            self.db.add(row)
        self.db.commit()

    def ensure_registered(self, agent_names: list[str]) -> None:
        """Insert any missing agents with enabled=1 (default).

        Existing entries are not modified.
        """
        existing = {
            row.agent_name
            for row in self.db.query(AgentRegistry.agent_name).all()
        }
        for name in agent_names:
            if name not in existing:
                self.db.add(AgentRegistry(agent_name=name, enabled=1))
        self.db.commit()
