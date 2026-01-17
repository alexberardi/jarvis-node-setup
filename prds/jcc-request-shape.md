# Jarvis Command Center Tool-Based Request Shape

This PRD documents the **tool-based request/response shape** used by Jarvis Command Center and the **routes it applies to**.

## Applies To (Routes)
- `POST /api/v0/conversation/start`
- `POST /api/v0/voice/command`
- `POST /api/v0/voice/command/continue`

## 1) Start Conversation
**Route:** `POST /api/v0/conversation/start`  
**Purpose:** Initialize a tool-based conversation and warm the model.

### Request: `ConversationStartRequest`
```json
{
  "conversation_id": "uuid-or-client-id",
  "node_context": {
    "timezone": "America/New_York"
  },
  "available_commands": [
    {
      "command_name": "get_weather",
      "description": "Get current weather or forecast.",
      "parameters": [
        {"name": "city", "type": "string", "required": false},
        {"name": "unit_system", "type": "string", "required": false}
      ],
      "rules": [],
      "critical_rules": [],
      "examples": [],
      "allow_direct_answer": false
    }
  ],
  "client_tools": [
    {
      "type": "function",
      "function": {
        "name": "get_weather",
        "description": "Get current weather or forecast.",
        "parameters": {
          "type": "object",
          "properties": {
            "city": { "type": "string" },
            "unit_system": { "type": "string" }
          },
          "required": []
        }
      },
      "allow_direct_answer": false
    }
  ]
}
```

**Notes**
- `node_context` is optional; server-side node context is used for security.  
- `available_commands` is **command metadata** (structured) for server tools like `get_command_utterance_examples`.  
- `client_tools` is **OpenAI tool format**, optionally extended with `allow_direct_answer`.  
- `allow_direct_answer: false` means **must call tool**; `true` means **direct answer allowed**.

## 2) Send Voice Command
**Route:** `POST /api/v0/voice/command`  
**Purpose:** Submit a user utterance for tool selection + parameter extraction.

### Request: `VoiceCommandRequest`
```json
{
  "voice_command": "What's the forecast for Los Angeles tomorrow?",
  "conversation_id": "uuid-or-client-id"
}
```

### Response: `VoiceCommandResponse`
```json
{
  "commands": [],
  "request_information": {
    "voice_command": "What's the forecast for Los Angeles tomorrow?",
    "conversation_id": "uuid-or-client-id"
  },
  "stop_reason": "tool_calls",
  "assistant_message": "brief ack",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "get_weather",
        "arguments": "{\"city\":\"Los Angeles\",\"resolved_datetimes\":[\"2026-01-17T05:00:00Z\"]}"
      }
    }
  ],
  "validation_request": null
}
```

**Stop reasons**
- `tool_calls` → client must execute tools and call `/voice/command/continue`.
- `validation_required` → server requests clarification.
- `complete` → final response (no tool calls).

## 3) Continue With Tool Results
**Route:** `POST /api/v0/voice/command/continue`  
**Purpose:** Send results of client-executed tools back to JCC to continue.

### Request: `ToolResultRequest`
```json
{
  "conversation_id": "uuid-or-client-id",
  "tool_results": [
    {
      "tool_call_id": "call_abc123",
      "output": {
        "forecast": "Sunny",
        "high": 72,
        "low": 58
      }
    }
  ]
}
```

### Response: `VoiceCommandResponse`
Same shape as above. If more tool calls are needed, `stop_reason` will be `tool_calls` again.

## Direct Answer Policy
- **allow_direct_answer: false** → tool must be called; no direct completion.
- **allow_direct_answer: true** → direct completion permitted.
- **allow_direct_answer: null/omitted** → default model behavior (no explicit rule).

## Non-Goals
- This PRD does **not** redefine the LLM proxy `/v1/chat/completions` contract.
