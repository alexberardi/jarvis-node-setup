from typing import Optional, List
from uuid import UUID
from sqlalchemy.orm import Session

from models.chunked_command_response import (
    ChunkedCommandResponse, 
    CreateChunkedCommandResponse, 
    UpdateChunkedCommandResponse,
    ChunkedCommandResponseDB
)


class ChunkedCommandResponseRepository:
    """
    Repository for managing chunked command responses in the database.
    
    Handles CRUD operations for commands that generate content incrementally.
    """
    
    def __init__(self, db: Session):
        """Initialize the repository with a database session."""
        self.db = db
    
    def create(self, create_data: CreateChunkedCommandResponse) -> ChunkedCommandResponse:
        """
        Create a new chunked command response session.
        
        Args:
            create_data: Data for creating the chunked response
            
        Returns:
            The created chunked command response
            
        Raises:
            Exception: If database operation fails
        """
        db_record = ChunkedCommandResponseDB(
            command_name=create_data.command_name,
            session_id=create_data.session_id,
            full_content=create_data.full_content
        )
        
        try:
            self.db.add(db_record)
            self.db.commit()
            self.db.refresh(db_record)
            
            return ChunkedCommandResponse.from_orm(db_record)
            
        except Exception as e:
            self.db.rollback()
            raise Exception(f"Failed to create chunked command response: {str(e)}")
    
    def get_by_session_id(self, session_id: str) -> Optional[ChunkedCommandResponse]:
        """
        Get a chunked command response by session ID.
        
        Args:
            session_id: The session identifier
            
        Returns:
            The chunked command response if found, None otherwise
            
        Raises:
            Exception: If database operation fails
        """
        try:
            db_record = self.db.query(ChunkedCommandResponseDB).filter_by(session_id=session_id).first()
            
            if db_record:
                return ChunkedCommandResponse.from_orm(db_record)
            return None
            
        except Exception as e:
            raise Exception(f"Failed to get chunked command response: {str(e)}")
    
    def update_content(self, session_id: str, update_data: UpdateChunkedCommandResponse) -> ChunkedCommandResponse:
        """
        Update the content of an existing chunked command response.
        
        Args:
            session_id: The session identifier
            update_data: Data for updating the content
            
        Returns:
            The updated chunked command response
            
        Raises:
            Exception: If database operation fails or session not found
        """
        try:
            db_record = self.db.query(ChunkedCommandResponseDB).filter_by(session_id=session_id).first()
            
            if not db_record:
                raise Exception(f"Session {session_id} not found")
            
            if update_data.append:
                # Append new content to existing content
                db_record.full_content += update_data.full_content
            else:
                # Replace existing content
                db_record.full_content = update_data.full_content
            
            self.db.commit()
            self.db.refresh(db_record)
            
            return ChunkedCommandResponse.from_orm(db_record)
            
        except Exception as e:
            self.db.rollback()
            raise Exception(f"Failed to update chunked command response: {str(e)}")
    
    def delete_by_session_id(self, session_id: str) -> bool:
        """
        Delete a chunked command response by session ID.
        
        Args:
            session_id: The session identifier
            
        Returns:
            True if deleted, False if not found
            
        Raises:
            Exception: If database operation fails
        """
        try:
            db_record = self.db.query(ChunkedCommandResponseDB).filter_by(session_id=session_id).first()
            
            if db_record:
                self.db.delete(db_record)
                self.db.commit()
                return True
            return False
            
        except Exception as e:
            self.db.rollback()
            raise Exception(f"Failed to delete chunked command response: {str(e)}")
    
    def get_by_command_name(self, command_name: str) -> List[ChunkedCommandResponse]:
        """
        Get all chunked command responses for a specific command.
        
        Args:
            command_name: The name of the command
            
        Returns:
            List of chunked command responses
            
        Raises:
            Exception: If database operation fails
        """
        try:
            db_records = self.db.query(ChunkedCommandResponseDB).filter_by(command_name=command_name).order_by(ChunkedCommandResponseDB.created_at.desc()).all()
            
            return [ChunkedCommandResponse.from_orm(record) for record in db_records]
            
        except Exception as e:
            raise Exception(f"Failed to get chunked command responses: {str(e)}")
    
    def cleanup_old_sessions(self, days_old: int = 7) -> int:
        """
        Clean up old chunked command response sessions.
        
        Args:
            days_old: Number of days old to consider for cleanup
            
        Returns:
            Number of sessions deleted
            
        Raises:
            Exception: If database operation fails
        """
        try:
            from datetime import datetime, timedelta
            
            cutoff_date = datetime.now() - timedelta(days=days_old)
            
            deleted_count = self.db.query(ChunkedCommandResponseDB).filter(
                ChunkedCommandResponseDB.created_at < cutoff_date
            ).delete()
            
            self.db.commit()
            return deleted_count
            
        except Exception as e:
            self.db.rollback()
            raise Exception(f"Failed to cleanup old sessions: {str(e)}")
