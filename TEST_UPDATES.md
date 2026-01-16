# Test Script Updates for Tool Calling Flow

## Overview
Updated test scripts to provide full visibility into the tool calling conversation flow, showing:
1. What data we send to the server (command response/context data)
2. How it's formatted as a tool result
3. What the server responds with (the message that will be spoken to the user)

## Files Updated

### `utils/test_command.py` - UPDATED ✅

**Purpose:** Test individual commands end-to-end with full tool calling flow

**New Flow:**
1. **Initial Request** → Server (voice command + conversation_id)
2. **Server Response** → Tool calls with command name and parameters
3. **Command Execution** → Execute command locally, get `CommandResponse`
4. **Format Tool Result** → Convert `CommandResponse` to tool result format
5. **Send Tool Results** → Server (tool results via `/api/v0/voice/command/continue`)
6. **Final Server Response** → What will be spoken to the user

**New Output Sections:**
```
=== COMMAND RESPONSE (Sent to Server) ===
- Shows the raw CommandResponse from executing the tool
- Displays context_data that will be used by server LLM
- Shows success status, error details, etc.

=== TOOL RESULT (Formatted for API) ===
- Shows JSON payload that will be sent to server
- Includes tool_call_id, success, context, and error fields

=== SERVER RESPONSE (What will be spoken) ===
- Shows the server's final response after processing tool results
- Displays the assistant_message that will be spoken to the user
- Shows stop_reason (complete, tool_calls, validation_required)
- Indicates if there are follow-up tool calls or validation requests
```

**Key Changes:**
- Added imports for `json`, `format_tool_result`, `format_tool_error`
- After command execution, formats the result as a tool result
- Sends tool results back to server via `jcc_client.send_tool_results()`
- Prints the server's response including the spoken message
- Handles multi-turn conversations (warns if more tool calls requested)
- Handles validation requests (shows validation question)

**Usage:**
```bash
python3 utils/test_command.py "What's the weather in Miami?"
```

**Example Output Flow:**
```
🔍 Starting command discovery...
⏱️  Command discovery completed in 0.345 seconds

=== COMMAND DISCOVERY RESPONSE ===
Stop Reason: tool_calls
Assistant Message: I'll check the weather in Miami for you.

--- EXECUTING COMMAND 1 ---
Command name: open_weather_command
Parameters: {'city': 'Miami'}

=== COMMAND RESPONSE (Sent to Server) ===
Success: True
Context Data Keys: ['temperature', 'conditions', 'city', 'humidity', 'wind_speed']
  temperature: 75
  conditions: partly cloudy
  city: Miami
  humidity: 68
  wind_speed: 12

=== TOOL RESULT (Formatted for API) ===
{
  "tool_call_id": "call_abc123",
  "output": {
    "success": true,
    "context": {
      "temperature": 75,
      "conditions": "partly cloudy",
      "city": "Miami"
      ...
    }
  }
}

📤 Sending tool results back to server...

=== SERVER RESPONSE (What will be spoken) ===
Stop Reason: complete
🔊 Assistant Message: It's currently 75 degrees and partly cloudy in Miami. The humidity is at 68% with winds at 12 miles per hour.
```

### `test_command_parsing.py` - NO CHANGES

**Purpose:** Test command parsing accuracy (parameter extraction from natural language)

**Why no changes:**
- This script tests **parsing only**, not execution
- It validates that the LLM correctly identifies command names and parameters
- Does not execute actual commands, so there are no tool results to send
- Focused on testing the `/api/v0/voice/command` response (command discovery)
- Does not need the full tool calling loop since it's not executing tools

**Current Output:**
```
🧪 Testing: Current weather with city specified
   Voice Command: 'What's the weather in Miami?'
   Expected Command: open_weather_command
   Expected Params: {'city': 'Miami'}
   📡 Response received: {...}
   🔧 Tool calling response detected
   ✅ Command name matches: open_weather_command
   ✅ All expected parameters match
   ✅ Test PASSED (⏱️  0.42s)
```

**This is appropriate because:**
- The script's goal is to verify parsing accuracy, not end-to-end execution
- It tests the LLM's ability to extract command names and parameters
- Adding tool execution would slow down the test suite significantly
- Parsing validation is a distinct concern from execution validation

## Key Differences Between Test Scripts

| Feature | test_command.py | test_command_parsing.py |
|---------|----------------|------------------------|
| Purpose | End-to-end testing | Parsing accuracy testing |
| Executes Commands | ✅ Yes | ❌ No |
| Sends Tool Results | ✅ Yes | ❌ No |
| Shows Server Response | ✅ Yes | ❌ No |
| Tests Multiple Commands | ❌ No (one at a time) | ✅ Yes (batch testing) |
| Performance Metrics | ❌ Basic timing | ✅ Detailed metrics |
| Focus | Full conversation flow | Parameter extraction |

## Testing Workflow

### For End-to-End Testing (with server responses):
```bash
# Test a single command with full flow
python3 utils/test_command.py "What's the weather in Miami?"

# See the full tool calling conversation including:
# - Initial command discovery
# - Local command execution  
# - Tool result formatting
# - Server's final spoken response
```

### For Parsing Validation (batch testing):
```bash
# Test all parsing tests
python3 test_command_parsing.py

# Test specific tests
python3 test_command_parsing.py -t 5 7 11

# Test specific commands
python3 test_command_parsing.py -c open_weather_command

# List all tests
python3 test_command_parsing.py -l
```

## Benefits

1. **Full Visibility:** See exactly what data flows between client and server
2. **Debugging:** Identify where issues occur in the conversation flow
3. **Server Message Preview:** See what will actually be spoken to the user
4. **Tool Result Validation:** Verify tool results are formatted correctly
5. **Multi-turn Detection:** Know when the server requests more tool calls
6. **Validation Detection:** See when the server needs user clarification

## Future Enhancements

### Potential additions to `test_command.py`:
- Support multi-turn conversations (execute multiple rounds of tool calls)
- Handle validation requests interactively
- Add performance metrics similar to test_command_parsing.py
- Support batch testing of multiple commands

### Potential additions to `test_command_parsing.py`:
- Optional flag to execute commands and show server responses
- Add tool result formatting validation
- Verify server responses match expected patterns

