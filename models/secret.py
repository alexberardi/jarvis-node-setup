import uuid

from sqlalchemy import Column, String, Text, DateTime
from models import Base


class Secret(Base):
    __tablename__ = "secrets"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    key = Column(String(255), nullable=False)
    value = Column(Text, nullable=False)
    scope = Column(String(255), nullable=False)
    value_type = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    updated_at = Column(DateTime(timezone=True), nullable=False)