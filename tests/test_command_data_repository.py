"""
Unit tests for CommandDataRepository.

Tests the generic command data persistence layer including
CRUD operations, expiration handling, and JSON serialization.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from models.command_data import CommandData
from repositories.command_data_repository import CommandDataRepository


@pytest.fixture
def mock_session():
    """Create a mock database session"""
    session = MagicMock()
    session.query.return_value = session
    session.filter_by.return_value = session
    session.filter.return_value = session
    return session


@pytest.fixture
def repository(mock_session):
    """Create a CommandDataRepository with mock session"""
    return CommandDataRepository(mock_session)


class TestCommandDataRepositorySave:
    """Tests for save method"""

    def test_save_creates_new_record(self, repository, mock_session):
        """Test saving new command data creates a record"""
        mock_session.filter_by.return_value.first.return_value = None

        data = {"timer_id": "abc123", "label": "pasta"}
        result = repository.save("set_timer", "abc123", data)

        mock_session.add.assert_called_once()
        mock_session.commit.assert_called()

    def test_save_updates_existing_record(self, repository, mock_session):
        """Test saving existing key updates the record"""
        existing = MagicMock()
        existing.data = '{"old": "data"}'
        mock_session.filter_by.return_value.first.return_value = existing

        data = {"timer_id": "abc123", "label": "new_label"}
        repository.save("set_timer", "abc123", data)

        assert existing.data == json.dumps(data)
        mock_session.add.assert_not_called()
        mock_session.commit.assert_called()

    def test_save_with_expiration(self, repository, mock_session):
        """Test saving with expiration date"""
        mock_session.filter_by.return_value.first.return_value = None

        expires = datetime.now(timezone.utc) + timedelta(hours=1)
        data = {"key": "value"}
        repository.save("test_cmd", "key1", data, expires_at=expires)

        mock_session.add.assert_called_once()
        added_record = mock_session.add.call_args[0][0]
        assert added_record.expires_at == expires

    def test_save_serializes_json(self, repository, mock_session):
        """Test that data is JSON serialized"""
        mock_session.filter_by.return_value.first.return_value = None

        data = {"nested": {"key": [1, 2, 3]}, "number": 42}
        repository.save("cmd", "key", data)

        added_record = mock_session.add.call_args[0][0]
        assert added_record.data == json.dumps(data)


class TestCommandDataRepositoryGet:
    """Tests for get method"""

    def test_get_returns_data(self, repository, mock_session):
        """Test getting existing record returns data"""
        record = MagicMock()
        record.data = '{"timer_id": "abc123", "label": "pasta"}'
        record.expires_at = None
        mock_session.filter_by.return_value.first.return_value = record

        result = repository.get("set_timer", "abc123")

        assert result == {"timer_id": "abc123", "label": "pasta"}

    def test_get_returns_none_for_missing(self, repository, mock_session):
        """Test getting non-existent record returns None"""
        mock_session.filter_by.return_value.first.return_value = None

        result = repository.get("set_timer", "nonexistent")

        assert result is None

    def test_get_returns_none_for_expired(self, repository, mock_session):
        """Test that expired records return None by default"""
        record = MagicMock()
        record.data = '{"key": "value"}'
        record.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        mock_session.filter_by.return_value.first.return_value = record

        result = repository.get("cmd", "key")

        assert result is None

    def test_get_with_include_expired(self, repository, mock_session):
        """Test that include_expired=True returns expired records"""
        record = MagicMock()
        record.data = '{"key": "value"}'
        record.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
        mock_session.filter_by.return_value.first.return_value = record

        result = repository.get("cmd", "key", include_expired=True)

        assert result == {"key": "value"}

    def test_get_handles_naive_datetime(self, repository, mock_session):
        """Test that naive datetimes are handled correctly"""
        record = MagicMock()
        record.data = '{"key": "value"}'
        # Naive datetime in the past
        record.expires_at = datetime.now() - timedelta(hours=1)
        record.expires_at = record.expires_at.replace(tzinfo=None)
        mock_session.filter_by.return_value.first.return_value = record

        result = repository.get("cmd", "key")

        # Should be treated as expired
        assert result is None


class TestCommandDataRepositoryGetAll:
    """Tests for get_all method"""

    def test_get_all_returns_list(self, repository, mock_session):
        """Test getting all records for a command"""
        record1 = MagicMock()
        record1.data = '{"id": "1"}'
        record1.data_key = "key1"
        record1.expires_at = None

        record2 = MagicMock()
        record2.data = '{"id": "2"}'
        record2.data_key = "key2"
        record2.expires_at = None

        mock_session.filter_by.return_value.all.return_value = [record1, record2]

        results = repository.get_all("set_timer")

        assert len(results) == 2
        assert results[0]["_data_key"] == "key1"
        assert results[1]["_data_key"] == "key2"

    def test_get_all_filters_expired(self, repository, mock_session):
        """Test that get_all filters expired records by default"""
        active = MagicMock()
        active.data = '{"id": "active"}'
        active.data_key = "active"
        active.expires_at = datetime.now(timezone.utc) + timedelta(hours=1)

        expired = MagicMock()
        expired.data = '{"id": "expired"}'
        expired.data_key = "expired"
        expired.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        mock_session.filter_by.return_value.all.return_value = [active, expired]

        results = repository.get_all("cmd")

        assert len(results) == 1
        assert results[0]["id"] == "active"

    def test_get_all_include_expired(self, repository, mock_session):
        """Test get_all with include_expired=True"""
        active = MagicMock()
        active.data = '{"id": "active"}'
        active.data_key = "active"
        active.expires_at = None

        expired = MagicMock()
        expired.data = '{"id": "expired"}'
        expired.data_key = "expired"
        expired.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)

        mock_session.filter_by.return_value.all.return_value = [active, expired]

        results = repository.get_all("cmd", include_expired=True)

        assert len(results) == 2

    def test_get_all_empty(self, repository, mock_session):
        """Test get_all returns empty list when no records"""
        mock_session.filter_by.return_value.all.return_value = []

        results = repository.get_all("cmd")

        assert results == []


class TestCommandDataRepositoryDelete:
    """Tests for delete methods"""

    def test_delete_returns_true_when_found(self, repository, mock_session):
        """Test delete returns True when record is deleted"""
        mock_session.filter_by.return_value.delete.return_value = 1

        result = repository.delete("cmd", "key")

        assert result is True
        mock_session.commit.assert_called()

    def test_delete_returns_false_when_not_found(self, repository, mock_session):
        """Test delete returns False when record not found"""
        mock_session.filter_by.return_value.delete.return_value = 0

        result = repository.delete("cmd", "nonexistent")

        assert result is False

    def test_delete_all(self, repository, mock_session):
        """Test delete_all removes all records for command"""
        mock_session.filter_by.return_value.delete.return_value = 5

        result = repository.delete_all("set_timer")

        assert result == 5
        mock_session.commit.assert_called()

    def test_delete_expired(self, repository, mock_session):
        """Test delete_expired removes expired records"""
        mock_session.filter.return_value.filter.return_value.delete.return_value = 3

        result = repository.delete_expired()

        assert result == 3
        mock_session.commit.assert_called()
