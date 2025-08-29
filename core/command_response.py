from dataclasses import dataclass
from typing import Any, Optional, Dict


@dataclass
class CommandResponse:
    """
    Normalized response structure for all Jarvis commands
    
    This object provides a consistent interface for command responses that supports
    conversational flows and context preservation for follow-up questions.
    """
    
    # What Jarvis will speak to the user
    speak_message: str
    
    # Whether Jarvis should wait for follow-up input
    wait_for_input: bool = True
    
    # The data found/processed by the command (for follow-up context)
    context_data: Optional[Dict[str, Any]] = None
    
    # Whether the command executed successfully
    success: bool = True
    
    # Any error details (for later validation handlers)
    error_details: Optional[str] = None
    
    # Command-specific metadata (optional)
    metadata: Optional[Dict[str, Any]] = None
    
    # Chunked response support
    is_chunked_response: bool = False
    chunk_session_id: Optional[str] = None
    
    def __post_init__(self):
        """Validate the response structure"""
        if not self.speak_message:
            raise ValueError("speak_message cannot be empty")
        
        # If there's an error, success should be False
        if self.error_details and self.success:
            self.success = False
    
    @classmethod
    def success_response(
        cls, 
        speak_message: str, 
        context_data: Optional[Dict[str, Any]] = None,
        wait_for_input: bool = True,
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a successful command response"""
        return cls(
            speak_message=speak_message,
            wait_for_input=wait_for_input,
            context_data=context_data,
            success=True,
            metadata=metadata
        )
    
    @classmethod
    def error_response(
        cls, 
        speak_message: str, 
        error_details: str,
        context_data: Optional[Dict[str, Any]] = None,
        wait_for_input: bool = False
    ) -> 'CommandResponse':
        """Create an error command response"""
        return cls(
            speak_message=speak_message,
            wait_for_input=wait_for_input,
            context_data=context_data,
            success=False,
            error_details=error_details
        )
    
    @classmethod
    def follow_up_response(
        cls, 
        speak_message: str, 
        context_data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a response that expects follow-up input"""
        return cls(
            speak_message=speak_message,
            wait_for_input=True,
            context_data=context_data,
            success=True,
            metadata=metadata
        )
    
    @classmethod
    def final_response(
        cls, 
        speak_message: str, 
        context_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a response that doesn't expect follow-up input"""
        return cls(
            speak_message=speak_message,
            wait_for_input=False,
            context_data=context_data,
            success=True,
            metadata=metadata
        )
    
    @classmethod
    def chunked_response(
        cls,
        speak_message: str,
        session_id: str,
        context_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a response for chunked content that can be continued"""
        return cls(
            speak_message=speak_message,
            wait_for_input=True,
            context_data=context_data,
            success=True,
            metadata=metadata,
            is_chunked_response=True,
            chunk_session_id=session_id
        )
