from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field
from sqlalchemy import Column, String, Text, DateTime, func

from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()


class ChunkedCommandResponseDB(Base):
    """SQLAlchemy model for the database table"""
    __tablename__ = "chunked_command_responses"
    
    id = Column(String(36), primary_key=True)
    command_name = Column(String(255), nullable=False)
    session_id = Column(String(255), nullable=False, unique=True)
    full_content = Column(Text, nullable=False, default="")
    created_at = Column(DateTime(timezone=True), server_default=func.current_timestamp())
    updated_at = Column(DateTime(timezone=True), server_default=func.current_timestamp(), onupdate=func.current_timestamp())


class ChunkedCommandResponse(BaseModel):
    """Pydantic model for API responses"""
    id: str = Field(..., description="Unique identifier for the chunked command response session")
    command_name: str = Field(..., description="Name of the command that generated this response")
    session_id: str = Field(..., description="Unique session identifier for this chunked response")
    full_content: str = Field(default="", description="The complete content generated so far (accumulated chunks)")
    created_at: datetime = Field(..., description="When this chunked response session was first created")
    updated_at: datetime = Field(..., description="When the content was last updated")
    
    class Config:
        from_attributes = True


class CreateChunkedCommandResponse(BaseModel):
    """Pydantic model for creating new chunked responses"""
    command_name: str = Field(..., description="Name of the command that generated this response")
    session_id: str = Field(..., description="Unique session identifier for this chunked response")
    full_content: str = Field(default="", description="Initial content for the first chunk")


class UpdateChunkedCommandResponse(BaseModel):
    """Pydantic model for updating chunked responses"""
    full_content: str = Field(..., description="New content to append to existing content")
    append: bool = Field(default=True, description="Whether to append content (True) or replace it (False)")


class ChunkedCommandResponseSession(BaseModel):
    """Pydantic model for in-memory session state"""
    session_id: str = Field(..., description="Session identifier matching the database record")
    last_spoken_token: int = Field(default=0, description="Position in content where we last stopped speaking")
    last_spoken_at: Optional[datetime] = Field(default=None, description="When we last spoke content from this session")
    
    def is_caught_up(self, db_updated_at: datetime) -> bool:
        if self.last_spoken_at is None:
            return False
        return self.last_spoken_at >= db_updated_at
    
    def get_new_content(self, full_content: str) -> str:
        if self.last_spoken_token >= len(full_content):
            return ""
        return full_content[self.last_spoken_token:]
    
    def mark_spoken(self, content_length: int):
        self.last_spoken_token += content_length
        self.last_spoken_at = datetime.now()
