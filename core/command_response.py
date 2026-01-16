from dataclasses import dataclass
from typing import Any, Optional, Dict


@dataclass
class CommandResponse:
    """
    Normalized response structure for all Jarvis commands
    
    This object provides a consistent interface for command responses that supports
    conversational flows and context preservation for follow-up questions.
    
    Note: The server generates the spoken response based on context_data.
    Commands should return raw data only.
    """
    
    # The data found/processed by the command (for server to use in generating response)
    context_data: Optional[Dict[str, Any]] = None
    
    # Whether the command executed successfully
    success: bool = True
    
    # Any error details (for later validation handlers)
    error_details: Optional[str] = None
    
    # Whether Jarvis should wait for follow-up input
    wait_for_input: bool = True
    
    # Command-specific metadata (optional)
    metadata: Optional[Dict[str, Any]] = None
    
    # Chunked response support
    is_chunked_response: bool = False
    chunk_session_id: Optional[str] = None
    
    def __post_init__(self):
        """Validate the response structure"""
        # If there's an error, success should be False
        if self.error_details and self.success:
            self.success = False
    
    @classmethod
    def success_response(
        cls, 
        context_data: Optional[Dict[str, Any]] = None,
        wait_for_input: bool = True,
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a successful command response"""
        return cls(
            context_data=context_data,
            success=True,
            wait_for_input=wait_for_input,
            metadata=metadata
        )
    
    @classmethod
    def error_response(
        cls, 
        error_details: str,
        context_data: Optional[Dict[str, Any]] = None,
        wait_for_input: bool = False
    ) -> 'CommandResponse':
        """Create an error command response"""
        return cls(
            context_data=context_data,
            success=False,
            error_details=error_details,
            wait_for_input=wait_for_input
        )
    
    @classmethod
    def follow_up_response(
        cls, 
        context_data: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a response that expects follow-up input"""
        return cls(
            context_data=context_data,
            success=True,
            wait_for_input=True,
            metadata=metadata
        )
    
    @classmethod
    def final_response(
        cls, 
        context_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a response that doesn't expect follow-up input"""
        return cls(
            context_data=context_data,
            success=True,
            wait_for_input=False,
            metadata=metadata
        )
    
    @classmethod
    def chunked_response(
        cls,
        session_id: str,
        context_data: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None
    ) -> 'CommandResponse':
        """Create a response for chunked content that can be continued"""
        return cls(
            context_data=context_data,
            success=True,
            wait_for_input=True,
            metadata=metadata,
            is_chunked_response=True,
            chunk_session_id=session_id
        )
