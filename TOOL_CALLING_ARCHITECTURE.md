# Tool Calling Architecture

This document describes the tool calling architecture implemented in the Jarvis client.

## Overview

The system has been refactored from a direct command execution model to an LLM tool-calling model with multi-turn conversations. This enables more flexible interactions, including:

- Dynamic tool selection by the LLM
- Multi-turn conversations with tool execution loops
- User validation/clarification flows
- Server-side and client-side tool execution

## Architecture

### Flow Diagram

```
User speaks → Wake word detected → STT → Voice Command
                                              ↓
                                    Command Execution Service
                                              ↓
                                    Generate conversation_id
                                              ↓
                                    Register available tools (optional)
                                              ↓
                                    Send to Command Center API
                                              ↓
                          ┌─────────────────────────────────────┐
                          │   Command Center API Response       │
                          │   (stop_reason determines action)   │
                          └─────────────────────────────────────┘
                                              ↓
                          ┌─────────────────────────────────────┐
                          │         stop_reason = ?             │
                          └─────────────────────────────────────┘
                                              ↓
                ┌─────────────────────┬───────────────────────┬─────────────────┐
                ↓                     ↓                       ↓
         "tool_calls"        "validation_required"       "complete"
                ↓                     ↓                       ↓
        Execute tools          Ask user for           Speak assistant_message
        locally              clarification                    ↓
                ↓                     ↓                      END
        Send tool            Capture user
        results back          response
                ↓                     ↓
                └────────► Send to /voice/command/continue ◄──────┘
                                     ↓
                          Loop back to
                          Command Center
                          API Response
```

## Key Components

### 1. ToolCallingResponse

**File:** `clients/responses/jarvis_command_center/tool_calling_response.py`

Pydantic model representing the API response:

```python
class ToolCallingResponse(BaseModel):
    stop_reason: str  # "tool_call", "validation", "stop", "complete"
    message: Optional[str]
    tool_calls: Optional[List[ToolCall]]
    validation: Optional[ValidationRequest]
    conversation_id: str
```

### 2. CommandExecutionService

**File:** `utils/command_execution_service.py`

Core service that orchestrates the tool calling flow:

- `process_voice_command()`: Main entry point that loops on stop_reason
- `register_tools_for_conversation()`: Registers available client-side tools
- `_execute_tools()`: Executes requested tools locally
- `_default_validation_handler()`: Placeholder for validation handling

### 3. JarvisCommandCenterClient

**File:** `clients/jarvis_command_center_client.py`

HTTP client for Command Center API:

- `send_command()`: Send initial voice command
- `send_tool_results()`: Send tool execution results back
- `send_validation_response()`: Send user's validation response back
- `start_conversation()`: Register available tools for a conversation

### 4. IJarvisCommand

**File:** `core/ijarvis_command.py`

Enhanced command interface with OpenAI compatibility:

- `to_openai_tool_schema()`: Converts command to OpenAI function calling format
- Supports all existing command properties (name, description, parameters, etc.)

### 5. Tool Result Formatter

**File:** `utils/tool_result_formatter.py`

Helper functions to format tool results:

- `format_tool_result()`: Format CommandResponse for API
- `format_tool_error()`: Format tool errors for API

## API Endpoints

### POST `/api/v0/conversation/start`

Register available client-side tools for a conversation.

**Request:**
```json
{
  "conversation_id": "uuid-here",
  "node_context": {
    "timezone": "America/New_York"
  },
  "client_tools": [
    {
      "type": "function",
      "function": {
        "name": "open_weather_command",
        "description": "Gets weather for a city",
        "parameters": {
          "type": "object",
          "properties": {
            "city": {"type": "string", "description": "City name"}
          },
          "required": []
        }
      }
    }
  ]
}
```

**Response:**
```json
{
  "status": "success",
  "conversation_id": "uuid-here"
}
```

### POST `/api/v0/voice/command`

Initial voice command submission.

**Request:**
```json
{
  "voice_command": "What's the weather like?",
  "conversation_id": "uuid",
  "node_context": {
    "node_id": "...",
    "room": "...",
    "timezone": "..."
  }
}
```

**Response (Tool-Based):**
```json
{
  "commands": [],
  "request_information": {
    "voice_command": "What's the weather like?",
    "conversation_id": "uuid"
  },
  "stop_reason": "tool_calls",
  "assistant_message": "Let me check the weather for you.",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "open_weather_command",
        "arguments": "{\"city\": \"Miami\"}"
      }
    }
  ],
  "validation_request": null
}
```

**Response (Complete):**
```json
{
  "commands": [],
  "request_information": {
    "voice_command": "What's the weather like?",
    "conversation_id": "uuid"
  },
  "stop_reason": "complete",
  "assistant_message": "It's currently 73 degrees and sunny in Miami.",
  "tool_calls": null,
  "validation_request": null
}
```

### POST `/api/v0/voice/command/continue`

Send tool execution results or validation responses to continue the conversation.

**Request (Tool Results):**
```json
{
  "conversation_id": "uuid",
  "tool_results": [
    {
      "tool_call_id": "call_abc123",
      "output": {
        "success": true,
        "message": "It's currently 73° and sunny in Miami.",
        "context": {...}
      }
    }
  ]
}
```

**Request (Validation Response):**
```json
{
  "conversation_id": "uuid",
  "validation_response": "Florida Panthers"
}
```

**Response:**
Same format as `/api/v0/voice/command` response. May return more tool calls, validation requests, or complete.

## Usage Example

### Basic Voice Command Processing

```python
from utils.command_execution_service import CommandExecutionService

service = CommandExecutionService()

# Process a voice command with tool calling
result = service.process_voice_command(
    "What's the weather in Miami?",
    register_tools=True  # Register available tools
)

print(result["message"])  # Speaks final response
```

### With Custom Validation Handler

```python
from clients.responses.jarvis_command_center import ValidationRequest

def my_validation_handler(validation: ValidationRequest) -> str:
    """Handle user validation via TTS and STT"""
    # Speak the question
    tts_provider.speak(False, validation.question)
    
    # Listen for response
    audio = listen()
    response = stt_provider.transcribe(audio)
    
    return response

result = service.process_voice_command(
    "Get me the score for the Panthers game",
    validation_handler=my_validation_handler
)
```

## Stop Reasons

| Stop Reason | Description | Client Action |
|-------------|-------------|---------------|
| `tool_calls` | LLM wants to call one or more tools | Execute tools locally, send results back via `/voice/command/continue` |
| `validation_required` | LLM needs user clarification | Prompt user, send response back via `/voice/command/continue` |
| `complete` | LLM has completed the request | Speak `assistant_message`, end conversation |

## Tool Definition Format

Commands are automatically converted to OpenAI function calling format:

**IJarvisCommand:**
```python
class MyCommand(IJarvisCommand):
    @property
    def command_name(self) -> str:
        return "my_command"
    
    @property
    def description(self) -> str:
        return "Does something useful"
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("city", "string", required=False)
        ]
```

**OpenAI Tool Schema (auto-generated):**
```json
{
  "type": "function",
  "function": {
    "name": "my_command",
    "description": "Does something useful",
    "parameters": {
      "type": "object",
      "properties": {
        "city": {
          "type": "string"
        }
      },
      "required": []
    }
  }
}
```

## Testing

Run the test script to verify tool calling works:

```bash
# Test tool registration
python test_tool_calling.py registration

# Test tool calling flow
python test_tool_calling.py calling

# Run all tests
python test_tool_calling.py
```

## Migration Notes

### What Changed

1. **CommandExecutionService**: Completely rewritten with conversation loop
2. **JarvisCommandCenterClient**: Added tool-calling specific methods
3. **IJarvisCommand**: Added `to_openai_tool_schema()` method
4. **Voice Listener**: Updated to support validation re-listening
5. **API Response Format**: Changed from command list to tool calling response

### What Stayed The Same

1. **IJarvisCommand interface**: Core properties unchanged
2. **Command implementations**: No changes needed to existing commands
3. **CommandResponse**: Existing response format maintained
4. **STT/TTS providers**: No changes

### Breaking Changes

- `JarvisCommandCenterClient.send_command()` now returns `ToolCallingResponse` instead of dict
- `CommandExecutionService.process_voice_command()` signature changed (added optional parameters)
- API response format changed (server must support new format)

## Future Enhancements

1. **Server-side tools**: Command Center can expose its own tools (date utilities, calculations, etc.)
2. **Tool result streaming**: Stream tool results back in real-time
3. **Multi-step workflows**: Chain multiple tool calls together
4. **Context preservation**: Maintain conversation context across multiple interactions
5. **Tool discovery**: Dynamic tool registration and discovery

