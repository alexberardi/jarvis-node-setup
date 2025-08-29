import uuid
from datetime import datetime
from typing import Optional, Tuple

from db import SessionLocal
from models.chunked_command_response import (
    ChunkedCommandResponse,
    CreateChunkedCommandResponse,
    UpdateChunkedCommandResponse,
    ChunkedCommandResponseSession
)
from repositories.chunked_command_response_repository import ChunkedCommandResponseRepository
from scripts.text_to_speech import speak


class ChunkedCommandResponseService:
    """
    Service for managing chunked command responses and handling the speaking logic.
    
    This service coordinates between commands that generate content incrementally
    and the speaking system that needs to read and speak that content.
    """
    
    def __init__(self):
        """Initialize the service with a repository."""
        self._active_sessions: dict[str, ChunkedCommandResponseSession] = {}
    
    def start_session(self, command_name: str, initial_content: str = "") -> str:
        """
        Start a new chunked command response session.
        
        Args:
            command_name: Name of the command starting the session
            initial_content: Initial content for the first chunk
            
        Returns:
            Session ID for the new session
        """
        session_id = str(uuid.uuid4())
        
        with SessionLocal() as session:
            repo = ChunkedCommandResponseRepository(session)
            # Create the database record
            create_data = CreateChunkedCommandResponse(
                command_name=command_name,
                session_id=session_id,
                full_content=initial_content
            )
            
            db_record = repo.create(create_data)
            
            # Create the in-memory session
            session_obj = ChunkedCommandResponseSession(session_id=session_id)
            self._active_sessions[session_id] = session_obj
            
            return session_id
    
    def append_content(self, session_id: str, new_content: str) -> ChunkedCommandResponse:
        """
        Append new content to an existing session.
        
        Args:
            session_id: The session identifier
            new_content: New content to append
            
        Returns:
            The updated database record
            
        Raises:
            Exception: If session not found or database operation fails
        """
        with SessionLocal() as session:
            repo = ChunkedCommandResponseRepository(session)
            update_data = UpdateChunkedCommandResponse(
                full_content=new_content,
                append=True
            )
            
            return repo.update_content(session_id, update_data)
    
    def replace_content(self, session_id: str, new_content: str) -> ChunkedCommandResponse:
        """
        Replace the content of an existing session.
        
        Args:
            session_id: The session identifier
            new_content: New content to replace existing content
            
        Returns:
            The updated database record
            
        Raises:
            Exception: If session not found or database operation fails
        """
        with SessionLocal() as session:
            repo = ChunkedCommandResponseRepository(session)
            update_data = UpdateChunkedCommandResponse(
                full_content=new_content,
                append=False
            )
            
            return repo.update_content(session_id, update_data)
    
    def speak_session(self, session_id: str) -> Tuple[str, bool]:
        """
        Speak the content from a session, starting from where we left off.
        
        This implements the "do-while" logic we discussed:
        1. Read current state from DB
        2. Speak new content
        3. Update speaking position
        4. Check if we're caught up
        5. Repeat if needed
        
        Args:
            session_id: The session identifier
            
        Returns:
            Tuple of (spoken_content, is_caught_up)
            
        Raises:
            Exception: If session not found or database operation fails
        """
        # Get the in-memory session
        session = self._active_sessions.get(session_id)
        if not session:
            raise Exception(f"Session {session_id} not found in active sessions")
        
        # Get the database record
        with SessionLocal() as db_session:
            repo = ChunkedCommandResponseRepository(db_session)
            db_record = repo.get_by_session_id(session_id)
            
            if not db_record:
                raise Exception(f"Session {session_id} not found in database")
            
            # Check if we're caught up
            if session.is_caught_up(db_record.updated_at):
                return "", True
            
            # Get new content to speak
            new_content = session.get_new_content(db_record.full_content)
            if not new_content:
                return "", True
            
            # Speak the new content
            speak(new_content)
            
            # Update the session state
            session.mark_spoken(len(new_content))
            
            # Check if we're now caught up
            is_caught_up = session.is_caught_up(db_record.updated_at)
            
            return new_content, is_caught_up
    
    def speak_session_until_caught_up(self, session_id: str) -> str:
        """
        Speak a session until we're caught up with the database.
        
        This is the main method that implements the complete do-while loop.
        
        Args:
            session_id: The session identifier
            
        Returns:
            All content that was spoken
            
        Raises:
            Exception: If session not found or database operation fails
        """
        all_spoken_content = []
        
        while True:
            spoken_content, is_caught_up = self.speak_session(session_id)
            
            if spoken_content:
                all_spoken_content.append(spoken_content)
            
            if is_caught_up:
                break
        
        return " ".join(all_spoken_content)
    
    def get_session_status(self, session_id: str) -> Optional[dict]:
        """
        Get the current status of a session.
        
        Args:
            session_id: The session identifier
            
        Returns:
            Dictionary with session status information, or None if not found
        """
        session = self._active_sessions.get(session_id)
        if not session:
            return None
        
        with SessionLocal() as db_session:
            repo = ChunkedCommandResponseRepository(db_session)
            db_record = repo.get_by_session_id(session_id)
            
            if not db_record:
                return None
            
            return {
                "session_id": session_id,
                "command_name": db_record.command_name,
                "total_content_length": len(db_record.full_content),
                "last_spoken_position": session.last_spoken_token,
                "remaining_content_length": len(db_record.full_content) - session.last_spoken_token,
                "is_caught_up": session.is_caught_up(db_record.updated_at),
                "last_spoken_at": session.last_spoken_at,
                "last_updated_at": db_record.updated_at,
                "created_at": db_record.created_at
            }
    
    def end_session(self, session_id: str) -> bool:
        """
        End a session and clean up resources.
        
        Args:
            session_id: The session identifier
            
        Returns:
            True if session was ended, False if not found
        """
        if session_id in self._active_sessions:
            del self._active_sessions[session_id]
            return True
        return False
    
    def cleanup_expired_sessions(self, days_old: int = 7) -> int:
        """
        Clean up expired sessions from the database.
        
        Args:
            days_old: Number of days old to consider for cleanup
            
        Returns:
            Number of sessions cleaned up
        """
        with SessionLocal() as session:
            repo = ChunkedCommandResponseRepository(session)
            return repo.cleanup_old_sessions(days_old)
    
    def get_active_sessions(self) -> list[str]:
        """
        Get list of active session IDs.
        
        Returns:
            List of active session IDs
        """
        return list(self._active_sessions.keys())
    
    def is_session_active(self, session_id: str) -> bool:
        """
        Check if a session is currently active.
        
        Args:
            session_id: The session identifier
            
        Returns:
            True if session is active, False otherwise
        """
        return session_id in self._active_sessions
