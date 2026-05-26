"""Unit tests for AgentRegistryRepository (mirrors command_registry pattern)."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base
from models.agent_registry import AgentRegistry
from repositories.agent_registry_repository import AgentRegistryRepository


@pytest.fixture
def db_session():
    """In-memory SQLite session for isolated repo tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


class TestSetEnabled:
    def test_insert_new_agent(self, db_session):
        repo = AgentRegistryRepository(db_session)
        repo.set_enabled("calendar_alerts", False)

        row = db_session.query(AgentRegistry).filter_by(agent_name="calendar_alerts").one()
        assert row.enabled == 0

    def test_update_existing(self, db_session):
        repo = AgentRegistryRepository(db_session)
        repo.set_enabled("calendar_alerts", True)
        repo.set_enabled("calendar_alerts", False)

        assert repo.get_all() == {"calendar_alerts": False}

    def test_toggle_back_on(self, db_session):
        repo = AgentRegistryRepository(db_session)
        repo.set_enabled("ha_snapshot", False)
        repo.set_enabled("ha_snapshot", True)

        assert repo.is_enabled("ha_snapshot") is True


class TestIsEnabled:
    def test_missing_agent_defaults_to_enabled(self, db_session):
        """Backward-compat: an agent not yet in the registry runs by default."""
        repo = AgentRegistryRepository(db_session)
        assert repo.is_enabled("never_registered") is True

    def test_disabled_returns_false(self, db_session):
        repo = AgentRegistryRepository(db_session)
        repo.set_enabled("noisy_agent", False)
        assert repo.is_enabled("noisy_agent") is False


class TestGetAll:
    def test_empty_returns_empty_dict(self, db_session):
        repo = AgentRegistryRepository(db_session)
        assert repo.get_all() == {}

    def test_returns_all_entries(self, db_session):
        repo = AgentRegistryRepository(db_session)
        repo.set_enabled("a", True)
        repo.set_enabled("b", False)
        repo.set_enabled("c", True)

        assert repo.get_all() == {"a": True, "b": False, "c": True}


class TestEnsureRegistered:
    def test_inserts_missing(self, db_session):
        repo = AgentRegistryRepository(db_session)
        repo.ensure_registered(["agent_x", "agent_y"])

        assert repo.get_all() == {"agent_x": True, "agent_y": True}

    def test_does_not_overwrite_existing(self, db_session):
        """ensure_registered must not reset a disabled agent back to enabled."""
        repo = AgentRegistryRepository(db_session)
        repo.set_enabled("agent_x", False)
        repo.ensure_registered(["agent_x", "agent_y"])

        assert repo.get_all() == {"agent_x": False, "agent_y": True}

    def test_empty_list_noop(self, db_session):
        repo = AgentRegistryRepository(db_session)
        repo.ensure_registered([])
        assert repo.get_all() == {}
