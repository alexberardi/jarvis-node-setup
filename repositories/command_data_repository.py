"""
Repository for generic command data persistence.

Provides CRUD operations for the command_data table, which can be used
by any command that needs to persist state between sessions.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from models.command_data import CommandData


class CommandDataRepository:
    """Repository for command data CRUD operations."""

    def __init__(self, db: Session):
        self.db = db

    def save(
        self,
        command_name: str,
        data_key: str,
        data: Dict[str, Any],
        expires_at: Optional[datetime] = None,
    ) -> CommandData:
        """
        Save or update command data.

        Uses upsert semantics: if a record with the same (command_name, data_key)
        exists, it will be updated. Otherwise, a new record is created.

        Args:
            command_name: The command that owns this data (e.g., "set_timer")
            data_key: Unique key within the command (e.g., timer_id)
            data: Dictionary to store (will be JSON-serialized)
            expires_at: Optional expiration time for auto-cleanup

        Returns:
            The saved CommandData record
        """
        now = datetime.now(timezone.utc)
        json_data = json.dumps(data)

        existing = (
            self.db.query(CommandData)
            .filter_by(command_name=command_name, data_key=data_key)
            .first()
        )

        if existing:
            existing.data = json_data
            existing.expires_at = expires_at
            existing.updated_at = now
            self.db.commit()
            return existing

        record = CommandData(
            id=str(uuid.uuid4()),
            command_name=command_name,
            data_key=data_key,
            data=json_data,
            expires_at=expires_at,
            created_at=now,
            updated_at=now,
        )
        self.db.add(record)
        self.db.commit()
        return record

    def get(
        self,
        command_name: str,
        data_key: str,
        include_expired: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """
        Get command data by key.

        Args:
            command_name: The command that owns this data
            data_key: The key to look up
            include_expired: If False (default), returns None for expired records

        Returns:
            The data dictionary, or None if not found/expired
        """
        record = (
            self.db.query(CommandData)
            .filter_by(command_name=command_name, data_key=data_key)
            .first()
        )

        if record is None:
            return None

        # Check expiration unless explicitly including expired
        if not include_expired and record.expires_at is not None:
            now = datetime.now(timezone.utc)
            # Handle naive datetimes by assuming UTC
            expires = record.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires < now:
                return None

        return json.loads(record.data)

    def get_all(
        self,
        command_name: str,
        include_expired: bool = False,
    ) -> List[Dict[str, Any]]:
        """
        Get all data for a command.

        Args:
            command_name: The command to get data for
            include_expired: If False (default), filters out expired records

        Returns:
            List of data dictionaries with 'data_key' added to each
        """
        query = self.db.query(CommandData).filter_by(command_name=command_name)
        records = query.all()

        results = []
        now = datetime.now(timezone.utc)

        for record in records:
            # Check expiration unless explicitly including expired
            if not include_expired and record.expires_at is not None:
                expires = record.expires_at
                if expires.tzinfo is None:
                    expires = expires.replace(tzinfo=timezone.utc)
                if expires < now:
                    continue

            data = json.loads(record.data)
            data["_data_key"] = record.data_key
            data["_expires_at"] = (
                record.expires_at.isoformat() if record.expires_at else None
            )
            results.append(data)

        return results

    def delete(self, command_name: str, data_key: str) -> bool:
        """
        Delete command data by key.

        Args:
            command_name: The command that owns this data
            data_key: The key to delete

        Returns:
            True if a record was deleted, False if not found
        """
        result = (
            self.db.query(CommandData)
            .filter_by(command_name=command_name, data_key=data_key)
            .delete()
        )
        self.db.commit()
        return result > 0

    def delete_all(self, command_name: str) -> int:
        """
        Delete all data for a command.

        Args:
            command_name: The command to delete data for

        Returns:
            Number of records deleted
        """
        result = (
            self.db.query(CommandData)
            .filter_by(command_name=command_name)
            .delete()
        )
        self.db.commit()
        return result

    def delete_expired(self) -> int:
        """
        Delete all expired records across all commands.

        Returns:
            Number of records deleted
        """
        now = datetime.now(timezone.utc)
        result = (
            self.db.query(CommandData)
            .filter(CommandData.expires_at.isnot(None))
            .filter(CommandData.expires_at < now)
            .delete()
        )
        self.db.commit()
        return result
