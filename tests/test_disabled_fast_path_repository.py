"""Unit tests for DisabledFastPathRepository."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base
from repositories.disabled_fast_path_repository import DisabledFastPathRepository


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    session = SessionLocal()
    yield session
    session.close()


class TestIsDisabled:
    def test_missing_pattern_defaults_to_enabled(self, db_session):
        repo = DisabledFastPathRepository(db_session)
        assert repo.is_disabled("set_timer", "timer.set") is False

    def test_disabled_returns_true(self, db_session):
        repo = DisabledFastPathRepository(db_session)
        repo.set_disabled("set_timer", "timer.set", True)
        assert repo.is_disabled("set_timer", "timer.set") is True

    def test_other_command_with_same_pattern_id_not_affected(self, db_session):
        """Composite PK ensures patterns scoped to a different command stay independent."""
        repo = DisabledFastPathRepository(db_session)
        repo.set_disabled("pandora", "play.station", True)
        assert repo.is_disabled("pandora", "play.station") is True
        assert repo.is_disabled("spotify", "play.station") is False


class TestSetDisabled:
    def test_disable_then_re_enable(self, db_session):
        repo = DisabledFastPathRepository(db_session)
        repo.set_disabled("set_timer", "timer.set", True)
        assert repo.is_disabled("set_timer", "timer.set") is True

        repo.set_disabled("set_timer", "timer.set", False)
        assert repo.is_disabled("set_timer", "timer.set") is False

    def test_disable_idempotent(self, db_session):
        """Disabling an already-disabled pattern must not raise."""
        repo = DisabledFastPathRepository(db_session)
        repo.set_disabled("x", "y", True)
        repo.set_disabled("x", "y", True)
        assert repo.is_disabled("x", "y") is True

    def test_enable_idempotent_when_not_present(self, db_session):
        """Enabling a not-disabled pattern is a no-op."""
        repo = DisabledFastPathRepository(db_session)
        repo.set_disabled("x", "y", False)
        assert repo.is_disabled("x", "y") is False


class TestGetDisabledIds:
    def test_empty_for_command_with_no_disabled(self, db_session):
        repo = DisabledFastPathRepository(db_session)
        assert repo.get_disabled_ids("set_timer") == set()

    def test_returns_all_disabled_for_command(self, db_session):
        repo = DisabledFastPathRepository(db_session)
        repo.set_disabled("set_timer", "timer.set", True)
        repo.set_disabled("set_timer", "timer.wake_me", True)
        repo.set_disabled("set_timer", "timer.notify", False)  # not disabled
        repo.set_disabled("other_cmd", "other.pattern", True)

        assert repo.get_disabled_ids("set_timer") == {"timer.set", "timer.wake_me"}


class TestGetAllDisabled:
    def test_empty_when_table_empty(self, db_session):
        repo = DisabledFastPathRepository(db_session)
        assert repo.get_all_disabled() == {}

    def test_groups_by_command_name(self, db_session):
        repo = DisabledFastPathRepository(db_session)
        repo.set_disabled("set_timer", "timer.set", True)
        repo.set_disabled("set_timer", "timer.wake_me", True)
        repo.set_disabled("pandora", "play.station", True)

        result = repo.get_all_disabled()
        assert result == {
            "set_timer": {"timer.set", "timer.wake_me"},
            "pandora": {"play.station"},
        }
