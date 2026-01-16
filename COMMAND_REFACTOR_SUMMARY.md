# Command Refactor Summary

## Changes Made

All commands have been updated to remove client-side message formatting. Commands now return raw data/context only. The server (Command Center) will handle generating natural language responses.

### Before
Commands would:
1. Execute their logic (fetch data from APIs, etc.)
2. Use `JarvisCommandCenterClient` to call the LLM to format a nice spoken response
3. Return `CommandResponse` with a formatted `speak_message`

### After
Commands now:
1. Execute their logic (fetch data from APIs, etc.)  
2. Return `CommandResponse` with empty `speak_message` and raw data in `context_data`
3. Server uses the context data to generate the response

## Updated Commands

### ✅ open_weather_command.py
- Removed LLM formatting for current weather
- Removed LLM formatting for forecast
- Returns raw weather data (temp, description, humidity, forecast details)

### ✅ general_knowledge_command.py  
- Removed entire LLM call
- Just passes query through to server
- Server will answer the question

### ✅ tell_a_joke_command.py
- Removed LLM joke generation
- Just passes topic through to server
- Server will generate the joke

### 🔄 web_search_command.py (TODO)
- Need to remove LLM response formatting
- Return raw search results

### 🔄 read_calendar_command.py (TODO)
- Need to remove LLM response formatting  
- Return raw calendar events

### 🔄 sports_score_command.py (TODO)
- Need to remove LLM response formatting
- Return raw game scores/schedules

### 🔄 story_command.py (TODO)
- Complex - uses streaming/chunking
- May need special handling

## Pattern for Updates

1. Remove imports: `JarvisCommandCenterClient`, `Config` (if only used for JCC)
2. Remove Pydantic response models (e.g., `KnowledgeResponse`, `JokeResponse`)
3. In `run()` method:
   - Remove LLM prompt creation
   - Remove `jcc_client` instantiation and calls
   - Change `speak_message` to empty string `""`
   - Keep all data in `context_data`

## Example

```python
# BEFORE
jcc_client = JarvisCommandCenterClient(Config.get("jarvis_command_center_api_url"))
response = jcc_client.chat(prompt, ResponseModel)
return CommandResponse.success_response(
    speak_message=response.message,
    context_data={"data": data}
)

# AFTER
return CommandResponse.success_response(
    speak_message="",  # Server will generate this
    context_data={"data": data}
)
```

