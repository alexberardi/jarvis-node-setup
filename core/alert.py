"""Alert model — lightweight dataclass for time-sensitive notifications.

Agents produce Alert objects; the AlertQueueService manages them in memory.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class Alert:
    """A time-sensitive notification produced by a background agent."""

    source_agent: str
    title: str
    summary: str
    created_at: datetime
    expires_at: datetime
    priority: int = 2  # 1=low, 2=medium, 3=high
    id: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > self.expires_at

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "source_agent": self.source_agent,
            "title": self.title,
            "summary": self.summary,
            "priority": self.priority,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
        }
