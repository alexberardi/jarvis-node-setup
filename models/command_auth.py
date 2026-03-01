from sqlalchemy import Column, String, Integer, Text

from models import Base


class CommandAuth(Base):
    """Tracks auth status per provider (e.g., "home_assistant", "spotify").

    Keyed by provider, not command_name, because multiple commands
    can share the same auth (all HA commands share "home_assistant").
    """

    __tablename__ = "command_auth"

    provider = Column(String(255), primary_key=True)
    needs_auth = Column(Integer, nullable=False, default=0)  # 0=false, 1=true
    auth_error = Column(Text, nullable=True)
    last_checked_at = Column(Text, nullable=True)  # ISO datetime
    last_authed_at = Column(Text, nullable=True)    # ISO datetime
