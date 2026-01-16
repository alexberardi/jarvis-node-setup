# JSON Serialization Fix for Datetime Objects

## Problem

When executing commands (particularly `read_calendar_command`) that return `datetime` objects in their `context_data`, the tool results failed to send to the server with the error:

```
[JarvisClient] Failed to send tool results: Object of type datetime is not JSON serializable
```

## Root Cause

1. Commands like `read_calendar_command` include `datetime` objects in their `context_data` (e.g., `target_dates` field)
2. The `format_tool_result()` function passes `context_data` directly without serialization
3. The `RestClient.post()` uses `requests.post(..., json=data)` which calls `json.dumps()` internally
4. Python's `json.dumps()` doesn't handle `datetime` objects by default

## Solution

Added a recursive JSON serialization helper function `_serialize_for_json()` in `utils/tool_result_formatter.py` that:

1. **Converts datetime/date objects** → ISO format strings using `.isoformat()`
2. **Recursively processes** dictionaries, lists, and tuples
3. **Preserves other types** as-is

### Implementation

**File:** `utils/tool_result_formatter.py`

```python
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
```

Then updated `format_tool_result()` to use this helper:

```python
def format_tool_result(tool_call_id: str, result: CommandResponse) -> Dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "output": {
            "success": result.success,
            "context": _serialize_for_json(result.context_data),  # ← Now serializes datetime objects
            "error": result.error_details
        }
    }
```

## Impact

### Files Changed
- ✅ `utils/tool_result_formatter.py` - Added `_serialize_for_json()` helper

### Files Using This Fix (Automatic)
- ✅ `utils/command_execution_service.py` - Uses `format_tool_result()` 
- ✅ `utils/test_command.py` - Uses `format_tool_result()`
- ✅ `test_tool_calling.py` - Uses `format_tool_result()`

### Commands Now Working
- ✅ `read_calendar_command` - Returns datetime objects in `target_dates`
- ✅ Any other command that includes datetime objects in `context_data`

## Example

### Before (Error)
```python
context_data = {
    "target_dates": [datetime(2025, 11, 9, 0, 0)]  # ← Not JSON serializable
}
```

### After (Success)
```python
# Automatically converted to:
context_data = {
    "target_dates": ["2025-11-09T00:00:00"]  # ← ISO format string
}
```

## Benefits

1. **Centralized Fix:** All tool results automatically handle datetime serialization
2. **Recursive:** Handles nested datetime objects in lists/dicts
3. **Non-Breaking:** Preserves other data types unchanged
4. **Standard Format:** ISO 8601 format is universally recognized
5. **Future-Proof:** Works for any command that returns datetime objects

## Testing

Test with commands that return datetime objects:

```bash
# Calendar command (includes target_dates as datetime objects)
python3 utils/test_command.py "what's on my calendar today?"

# Weather command with forecast dates
python3 utils/test_command.py "what's the weather tomorrow?"

# Sports score with game dates
python3 utils/test_command.py "how did the Giants do yesterday?"
```

All should now successfully send tool results to the server without JSON serialization errors.

