from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field
import json
import ast


class ToolCallFunction(BaseModel):
    """Function details within a tool call"""
    name: str = Field(..., description="Name of the function to call")
    arguments: str = Field(..., description="JSON string of arguments")
    
    def get_arguments_dict(self) -> Dict[str, Any]:
        """Parse arguments JSON string into a dictionary"""
        try:
            args = json.loads(self.arguments)
            
            # Fix for server-side double-encoding of list parameters
            # If any value is a string that looks like a list, try to parse it
            for key, value in args.items():
                if isinstance(value, str) and value.startswith('[') and value.endswith(']'):
                    try:
                        # Try to parse as JSON first
                        args[key] = json.loads(value)
                    except json.JSONDecodeError:
                        # If that fails, try to evaluate as Python literal
                        try:
                            args[key] = ast.literal_eval(value)
                        except (ValueError, SyntaxError):
                            # If all parsing fails, leave as string
                            pass
            
            return args
        except json.JSONDecodeError:
            return {}


class ToolCall(BaseModel):
    """Represents a tool call request from the LLM"""
    id: str = Field(..., description="Unique identifier for this tool call")
    type: str = Field(default="function", description="Type of tool call")
    function: ToolCallFunction = Field(..., description="Function to call with arguments")


class ValidationRequest(BaseModel):
    """Represents a validation/clarification request to the user"""
    question: str = Field(..., description="The question to ask the user")
    parameter_name: str = Field(..., description="The parameter the validation will satisfy")
    options: Optional[List[str]] = Field(None, description="Optional list of choices for the user")
    tool_call_id: str = Field(..., description="The tool call id this validation should satisfy")


class RequestInformationResponse(BaseModel):
    """Request information from the API response"""
    voice_command: str
    conversation_id: str


class ToolCallingResponse(BaseModel):
    """
    Response from the Command Center API for tool calling flow
    
    The stop_reason determines what the client should do next:
    - 'complete': Conversation finished, no action needed
    - 'tool_calls': Client must execute tools and call continue endpoint
    - 'validation_required': User clarification needed
    """
    commands: List[Any] = Field(default_factory=list, description="Legacy command format (optional)")
    request_information: Optional[RequestInformationResponse] = Field(None, description="Information about the request")
    stop_reason: Optional[str] = Field(None, description="Why the LLM stopped: 'complete', 'tool_calls', or 'validation_required'")
    assistant_message: Optional[str] = Field(None, description="Message from the assistant")
    tool_calls: Optional[List[ToolCall]] = Field(None, description="List of tools to execute (when stop_reason is 'tool_calls')")
    validation_request: Optional[ValidationRequest] = Field(None, description="Validation request (when stop_reason is 'validation_required')")
    
    def is_final(self) -> bool:
        """Check if this is a final response (conversation is complete)"""
        return self.stop_reason == "complete" if self.stop_reason else False
    
    def requires_tool_execution(self) -> bool:
        """Check if this response requires tool execution"""
        return self.stop_reason == "tool_calls" and self.tool_calls is not None and len(self.tool_calls) > 0
    
    def requires_validation(self) -> bool:
        """Check if this response requires user validation"""
        return self.stop_reason == "validation_required" and self.validation_request is not None
    
    @property
    def conversation_id(self) -> Optional[str]:
        """Get conversation ID from request information"""
        return self.request_information.conversation_id if self.request_information else None
    
    def is_error(self) -> bool:
        """Check if this response indicates an error (missing stop_reason)"""
        return self.stop_reason is None

