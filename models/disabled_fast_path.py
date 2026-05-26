from sqlalchemy import Column, DateTime, String, func

from models import Base


class DisabledFastPath(Base):
    """Per-pattern opt-out for `IJarvisCommand.fast_path_patterns`.

    Each row is a pattern the user has disabled from the mobile inspect UI.
    Patterns NOT in this table default to enabled, so newly-installed
    packages don't require an explicit insert. Storage is sparse — only the
    disabled set is recorded — keeping the table tiny.

    `command_name` + `pattern_id` together identify a specific pattern. Two
    different packages may safely use the same `pattern_id` locally because
    the command_name disambiguates them.
    """

    __tablename__ = "disabled_fast_paths"

    command_name = Column(String(255), primary_key=True)
    pattern_id = Column(String(255), primary_key=True)
    disabled_at = Column(DateTime, nullable=False, server_default=func.now())
