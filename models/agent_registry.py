from sqlalchemy import Column, String, Integer

from models import Base


class AgentRegistry(Base):
    """Tracks enabled/disabled state per agent.

    Agents not in this table default to enabled for backward compatibility.
    """

    __tablename__ = "agent_registry"

    agent_name = Column(String(255), primary_key=True)
    enabled = Column(Integer, nullable=False, default=1)  # 0=false, 1=true
