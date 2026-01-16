# Server Error Fix - More Resilient Response Handling

## Issue Encountered

The client was failing with a Pydantic validation error when the server returned an incomplete response:

```
[JarvisClient] Failed to send command: 1 validation error for ToolCallingResponse
stop_reason
  Input should be a valid string [type=string_type, input_value=None, input_type=NoneType]
```

Additionally, the conversation start endpoint was returning a 500 error:
```
[RestClient] Error during POST to http://10.0.0.103:9998/api/v0/conversation/start: 500 Server Error: Internal Server Error
```

## Root Cause

The server is either:
1. Not fully updated to the new tool calling API format yet
2. Encountering an error and returning a malformed response
3. Missing required configuration or dependencies

When the server returns a response without `stop_reason`, our strict Pydantic model was rejecting it entirely, making it harder to debug.

## Client-Side Fix Applied

Made the `ToolCallingResponse` model more resilient to handle server errors gracefully:

### Changes to `clients/responses/jarvis_command_center/tool_calling_response.py`:

1. **Made fields optional:**
   ```python
   # Before
   request_information: RequestInformationResponse = Field(...)
   stop_reason: str = Field(...)
   
   # After
   request_information: Optional[RequestInformationResponse] = Field(None)
   stop_reason: Optional[str] = Field(None)
   ```

2. **Updated helper methods to handle None values:**
   ```python
   def is_final(self) -> bool:
       """Check if this is a final response (conversation is complete)"""
       return self.stop_reason == "complete" if self.stop_reason else False
   
   @property
   def conversation_id(self) -> Optional[str]:
       """Get conversation ID from request information"""
       return self.request_information.conversation_id if self.request_information else None
   ```

3. **Added error detection method:**
   ```python
   def is_error(self) -> bool:
       """Check if this response indicates an error (missing stop_reason)"""
       return self.stop_reason is None
   ```

## Benefits

Now the client can:
- ✅ Parse incomplete server responses without crashing
- ✅ Detect error conditions with `response.is_error()`
- ✅ Provide better error messages for debugging
- ✅ Continue testing even when server is in development

## Server-Side Issues to Address

### 1. Conversation Start Endpoint (500 Error)

**Endpoint:** `POST /api/v0/conversation/start`

**Possible causes:**
- Database connection issues
- Missing required fields in the request
- Server configuration errors
- Dependency issues (LLM client, database, etc.)

**Check:**
- Server logs for stack traces
- Database connectivity
- Environment variables/configuration
- Required services (LLM proxy, database, etc.)

### 2. Command Endpoint Response Format

**Endpoint:** `POST /api/v0/voice/command`

**Issue:** Server returning response without `stop_reason`

**Expected response format:**
```json
{
  "commands": [],
  "request_information": {
    "voice_command": "Do the new york yankees play tomorrow?",
    "conversation_id": "4b5e7357-1d74-4461-87d1-b7aaa2e0c4cc"
  },
  "stop_reason": "tool_calls",
  "assistant_message": "I'll check the Yankees schedule for tomorrow.",
  "tool_calls": [
    {
      "id": "call_abc123",
      "type": "function",
      "function": {
        "name": "get_sports_schedule",
        "arguments": "{\"team_name\": \"New York Yankees\", \"datetimes\": [\"2025-11-10T00:00:00\"]}"
      }
    }
  ],
  "validation_request": null
}
```

**Check:**
- LLM is properly configured and responding
- Tool calling flow is implemented on server
- Error handling returns proper response structure

## Testing with Resilient Client

The client will now handle server errors more gracefully:

```bash
# Test command that was failing
python3 utils/test_command.py "Do the new york yankees play tomorrow?"
```

**Expected behavior now:**
- Client parses the response successfully
- Detects error condition with `response.is_error()`
- Logs clear error message instead of Pydantic validation error
- Allows for easier server-side debugging

## Next Steps

### Client-Side (✅ Complete):
- ✅ More resilient response parsing
- ✅ Better error detection
- ✅ Clearer error messages

### Server-Side (⚠️ Needs Attention):
1. **Fix conversation start endpoint** - Resolve 500 error
2. **Ensure proper response format** - Always include `stop_reason`
3. **Add error handling** - Return proper error responses even when internal errors occur
4. **Test tool calling flow** - Verify LLM proxy integration
5. **Check configuration** - Verify all required environment variables and dependencies

## Recommended Server-Side Response Structure for Errors

When the server encounters an error, it should still return a valid `ToolCallingResponse`:

```json
{
  "commands": [],
  "request_information": {
    "voice_command": "original command",
    "conversation_id": "conversation-id"
  },
  "stop_reason": "complete",
  "assistant_message": "I'm sorry, I encountered an error processing your request. Please try again.",
  "tool_calls": null,
  "validation_request": null
}
```

This allows the client to handle errors gracefully and provide user feedback.

## Debugging Commands

### Check server health:
```bash
curl http://10.0.0.103:9998/health
```

### Test conversation start:
```bash
curl -X POST http://10.0.0.103:9998/api/v0/conversation/start \
  -H "Content-Type: application/json" \
  -d '{
    "conversation_id": "test-123",
    "node_context": {"timezone": "America/New_York"},
    "client_tools": []
  }'
```

### Test command endpoint:
```bash
curl -X POST http://10.0.0.103:9998/api/v0/voice/command \
  -H "Content-Type: application/json" \
  -d '{
    "voice_command": "test command",
    "conversation_id": "test-123",
    "node_context": {"timezone": "America/New_York"}
  }'
```

## Summary

The client is now more resilient to server errors and will provide better debugging information. However, **the server needs to be fixed** to properly implement the tool calling API endpoints. Once the server is updated, the client should work correctly with the improved command descriptions we just implemented.

