from typing import Dict, Any
from datetime import datetime, date
from core.command_response import CommandResponse


def _serialize_for_json(obj: Any) -> Any:
    """
    Recursively convert objects to JSON-serializable formats
    
    Handles:
    - datetime/date objects → ISO format strings
    - Lists and dicts → recursively process
    - Other objects → convert to string
    """
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    elif isinstance(obj, dict):
        return {key: _serialize_for_json(value) for key, value in obj.items()}
    elif isinstance(obj, list):
        return [_serialize_for_json(item) for item in obj]
    elif isinstance(obj, tuple):
        return tuple(_serialize_for_json(item) for item in obj)
    else:
        return obj


def format_tool_result(
    tool_call_id: str,
    result: CommandResponse
) -> Dict[str, Any]:
    """
    Format a CommandResponse into a tool result for the API
    
    Args:
        tool_call_id: The ID of the tool call this result corresponds to
        result: The CommandResponse from executing the tool
        
    Returns:
        Dictionary formatted for sending back to the API in format:
        {"tool_call_id": "...", "output": {...}}
    """
    output: Dict[str, Any] = {
        "success": result.success,
    }

    # Promote "message" to top level so it's the first thing the LLM
    # reads — small models get confused parsing nested JSON.
    context = _serialize_for_json(result.context_data) if result.context_data else None
    if isinstance(context, dict) and "message" in context:
        output["message"] = context.pop("message")

    output["context"] = context
    output["error"] = result.error_details

    return {
        "tool_call_id": tool_call_id,
        "output": output,
    }


def format_tool_error(
    tool_call_id: str,
    error_message: str
) -> Dict[str, Any]:
    """
    Format a tool execution error for the API
    
    Args:
        tool_call_id: The ID of the tool call that failed
        error_message: Description of the error
        
    Returns:
        Dictionary formatted for sending back to the API in format:
        {"tool_call_id": "...", "output": {...}}
    """
    return {
        "tool_call_id": tool_call_id,
        "output": {
            "success": False,
            "error": error_message,
            "context": None
        }
    }

