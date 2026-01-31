"""
Mock responses for testing CommandExecutionService.

These factory functions create response dictionaries that match what the
JarvisCommandCenterClient returns from the Command Center API.
"""

from typing import Any, Dict, List, Optional


def create_complete_response(
    message: str,
    conversation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a complete/final response (no further action needed).

    Args:
        message: The assistant's final message
        conversation_id: Optional conversation ID

    Returns:
        Dict matching ToolCallingResponse schema with stop_reason="complete"
    """
    response = {
        "stop_reason": "complete",
        "assistant_message": message,
        "tool_calls": None,
        "validation_request": None,
        "commands": []
    }
    if conversation_id:
        response["request_information"] = {
            "voice_command": "",
            "conversation_id": conversation_id
        }
    return response


def create_tool_call_response(
    tool_calls: List[Dict[str, Any]],
    assistant_message: Optional[str] = None,
    conversation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a response that requires tool execution.

    Args:
        tool_calls: List of tool calls, each with:
            - name: Tool name
            - arguments: Dict of arguments (will be JSON-serialized)
            - id: Optional tool call ID (generated if not provided)
        assistant_message: Optional message from assistant
        conversation_id: Optional conversation ID

    Returns:
        Dict matching ToolCallingResponse schema with stop_reason="tool_calls"

    Example:
        create_tool_call_response([
            {"name": "calculate", "arguments": {"expression": "2+2"}}
        ])
    """
    import json
    import uuid

    formatted_calls = []
    for call in tool_calls:
        tool_call_id = call.get("id", f"call_{uuid.uuid4().hex[:12]}")
        arguments = call.get("arguments", {})

        formatted_calls.append({
            "id": tool_call_id,
            "type": "function",
            "function": {
                "name": call["name"],
                "arguments": json.dumps(arguments) if isinstance(arguments, dict) else arguments
            }
        })

    response = {
        "stop_reason": "tool_calls",
        "assistant_message": assistant_message,
        "tool_calls": formatted_calls,
        "validation_request": None,
        "commands": []
    }
    if conversation_id:
        response["request_information"] = {
            "voice_command": "",
            "conversation_id": conversation_id
        }
    return response


def create_validation_response(
    question: str,
    parameter_name: str,
    options: Optional[List[str]] = None,
    tool_call_id: Optional[str] = None,
    conversation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a response that requires user validation/clarification.

    Args:
        question: The question to ask the user
        parameter_name: The parameter being clarified
        options: Optional list of choices for the user
        tool_call_id: ID to associate with this validation
        conversation_id: Optional conversation ID

    Returns:
        Dict matching ToolCallingResponse schema with stop_reason="validation_required"

    Example:
        create_validation_response(
            question="Which city do you want weather for?",
            parameter_name="location",
            options=["New York", "Los Angeles", "Chicago"]
        )
    """
    import uuid

    response = {
        "stop_reason": "validation_required",
        "assistant_message": None,
        "tool_calls": None,
        "validation_request": {
            "question": question,
            "parameter_name": parameter_name,
            "options": options,
            "tool_call_id": tool_call_id or f"validation_{uuid.uuid4().hex[:8]}"
        },
        "commands": []
    }
    if conversation_id:
        response["request_information"] = {
            "voice_command": "",
            "conversation_id": conversation_id
        }
    return response


def create_error_response(
    error_message: str,
    conversation_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create an error response (missing stop_reason indicates error).

    Args:
        error_message: The error message
        conversation_id: Optional conversation ID

    Returns:
        Dict with error indicators
    """
    response = {
        "stop_reason": None,
        "assistant_message": error_message,
        "tool_calls": None,
        "validation_request": None,
        "commands": []
    }
    if conversation_id:
        response["request_information"] = {
            "voice_command": "",
            "conversation_id": conversation_id
        }
    return response
