"""
CommandData model for generic command persistence.

This table provides a flexible key-value store that any command can use to persist
data between sessions. The design is intentionally generic:
- command_name: identifies which command owns the data
- data_key: unique key within the command (e.g., timer_id, list_name)
- data: JSON blob for flexible schema per command
- expires_at: optional auto-cleanup for time-bounded data

Example uses:
- Timers: store timer state keyed by timer_id, expires when timer fires
- Shopping lists: store items keyed by list_name, no expiration
- Reminders: store reminder data with expiration date
"""

import uuid

from sqlalchemy import Column, DateTime, String, Text, UniqueConstraint

from models import Base


class CommandData(Base):
    """Generic command data persistence model."""

    __tablename__ = "command_data"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    command_name = Column(String(255), nullable=False, index=True)
    data_key = Column(String(255), nullable=False)
    data = Column(Text, nullable=False)  # JSON string
    expires_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("command_name", "data_key", name="uq_command_data_key"),
    )

    def __repr__(self) -> str:
        return f"<CommandData(command={self.command_name}, key={self.data_key})>"
