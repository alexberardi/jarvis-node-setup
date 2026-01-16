# API Format Updates

This document summarizes the updates made to match the exact server API format.

## Summary of Changes

### 1. Response Model Structure (`tool_calling_response.py`)

**Updated to match server format:**

```python
class ToolCallFunction(BaseModel):
    name: str  # Function name
    arguments: str  # JSON string (not dict)

class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function: ToolCallFunction  # Nested structure

class ToolCallingResponse(BaseModel):
    commands: List[Any] = []  # Legacy field
    request_information: RequestInformationResponse
    stop_reason: str  # "complete", "tool_calls", "validation_required"
    assistant_message: Optional[str]
    tool_calls: Optional[List[ToolCall]]
    validation_request: Optional[ValidationRequest]  # Not "validation"
```

**Key differences from initial implementation:**
- Tool call arguments are JSON strings, not dicts
- Tool calls have nested `function` structure
- Response includes `commands`, `request_information`, `assistant_message`
- Stop reasons: `complete`, `tool_calls`, `validation_required` (not `stop`, `tool_call`, `validation`)

### 2. API Endpoints

#### `/api/v0/conversation/start`

**Request format:**
```json
{
  "conversation_id": "uuid",
  "node_context": {"timezone": "America/New_York"},
  "client_tools": [...]  // Not "available_tools"
}
```

**Response format:**
```json
{
  "status": "success",
  "conversation_id": "uuid"
}
```

#### `/api/v0/voice/command`

**Response includes:**
- `commands` (legacy)
- `request_information` (contains voice_command and conversation_id)
- `stop_reason`
- `assistant_message`
- `tool_calls` (when stop_reason is "tool_calls")
- `validation_request` (when stop_reason is "validation_required")

#### `/api/v0/voice/command/continue`

**Single endpoint for both tool results AND validation responses:**

Tool results format:
```json
{
  "conversation_id": "uuid",
  "tool_results": [
    {
      "tool_call_id": "call_123",
      "output": {
        "success": true,
        "message": "...",
        "context": {...},
        "error": null
      }
    }
  ]
}
```

Validation response format:
```json
{
  "conversation_id": "uuid",
  "validation_response": "user's response"
}
```

### 3. Tool Result Formatting

**Updated format:**
```python
{
    "tool_call_id": "...",
    "output": {
        "success": bool,
        "message": str,
        "context": dict,
        "error": str or None
    }
}
```

**No longer includes:**
- `tool_name` field at top level
- Separate fields for result/success/context

### 4. Stop Reason Handling

**Server uses:**
- `complete` - conversation finished
- `tool_calls` - execute tools (plural)
- `validation_required` - user clarification needed

**Client checks:**
```python
response.is_final()  # stop_reason == "complete"
response.requires_tool_execution()  # stop_reason == "tool_calls"
response.requires_validation()  # stop_reason == "validation_required"
```

### 5. Tool Call Execution

**Parse arguments from JSON string:**
```python
tool_name = tool_call.function.name
arguments = tool_call.function.get_arguments_dict()  # Parses JSON string
```

### 6. Final Message Handling

**Use `assistant_message` from response:**
```python
final_message = response.assistant_message or "Task completed."
```

## Files Modified

1. `clients/responses/jarvis_command_center/tool_calling_response.py` - Updated models
2. `clients/responses/jarvis_command_center/__init__.py` - Updated exports
3. `clients/jarvis_command_center_client.py` - Updated endpoints and payloads
4. `utils/tool_result_formatter.py` - Updated result format
5. `utils/command_execution_service.py` - Updated to use new models
6. `TOOL_CALLING_ARCHITECTURE.md` - Updated documentation

## Testing

All files pass linting with zero errors. Ready for integration testing with the updated Command Center server.

