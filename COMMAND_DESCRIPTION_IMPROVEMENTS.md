# Command Description Improvements for Tool Calling

## Overview
Since examples are no longer exposed to the LLM, command and parameter descriptions must be comprehensive enough for the LLM to:
1. Select the correct command based on user intent
2. Extract the correct parameters
3. Distinguish between similar commands

## Key Principles for Good Descriptions

### Command Names
- **Clear action verbs**: What does it DO?
- **Consistent naming**: Use `_command` suffix for all commands or none
- **No ambiguity**: Name should hint at functionality

### Command Descriptions
- **Start with action verb**: "Gets...", "Converts...", "Calculates..."
- **Include use cases**: "Use this for...", "Use when..."
- **Include anti-patterns**: "Do NOT use for...", "This is NOT for..."
- **Distinguish from similar commands**: How is it different from X?
- **Be specific**: Include scope (sports leagues, weather types, etc.)

### Parameter Descriptions
- **Include expected format**: "ISO datetime string", "Team name with city"
- **Provide examples inline**: "(e.g., 'Miami', 'Tokyo')"
- **Clarify optionality**: When is it required vs optional?
- **Explain defaults**: What happens if not provided?
- **Specify valid values**: Enums, ranges, patterns

---

## Proposed Changes

### 1. ✅ calculator_command → **calculate**
**Current Name:** `calculator_command`  
**Proposed Name:** `calculate`  
**Reason:** Shorter, clearer action verb. "Calculate" is more direct than "calculator command"

**Current Description:**
> "Perform basic arithmetic operations (add, subtract, multiply, divide) on two numbers"

**Proposed Description:**
> "Perform basic arithmetic calculations with two numbers. Supports addition, subtraction, multiplication, and division. Use this for simple math operations. Do NOT use for unit conversions (use measurement_conversion_command) or complex mathematical formulas."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "num1", 
        "float", 
        required=True, 
        description="The first number in the calculation. Can be positive, negative, integer, or decimal (e.g., 5, -3.14, 42.5)"
    ),
    JarvisParameter(
        "num2", 
        "float", 
        required=True, 
        description="The second number in the calculation. Can be positive, negative, integer, or decimal (e.g., 3, -1.5, 100)"
    ),
    JarvisParameter(
        "operation", 
        "string", 
        required=True, 
        description="The arithmetic operation to perform. Must be exactly one of: 'add' (addition/sum), 'subtract' (subtraction/difference), 'multiply' (multiplication/product), 'divide' (division/quotient)"
    )
]
```

---

### 2. ✅ general_knowledge_command → **answer_question**
**Current Name:** `general_knowledge_command`  
**Proposed Name:** `answer_question`  
**Reason:** More intuitive - it answers questions. "General knowledge" is vague.

**Current Description:**
> "Answers general knowledge questions about established facts, history, geography, science, definitions, and well-known information. Use for factual questions about the world, not personal information like calendars, schedules, or user-specific data."

**Proposed Description:**
> "Answer factual questions about established knowledge including history, science, geography, definitions, famous people, and general facts. Use this for 'what is', 'who was', 'when did', 'where is', 'how does' questions about non-current information. Do NOT use for: current events or news (use web_search_command), weather (use get_weather), sports scores/schedules (use sports commands), personal calendar/events (use read_calendar_command), or calculations (use calculate)."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "query", 
        "string", 
        required=True, 
        description="The factual question to answer. Should be a complete question about established knowledge (e.g., 'What is the capital of France?', 'Who invented the telephone?', 'When did World War II end?', 'How does photosynthesis work?')"
    )
]
```

---

### 3. ✅ measurement_conversion_command → **convert_measurement**
**Current Name:** `measurement_conversion_command`  
**Proposed Name:** `convert_measurement`  
**Reason:** Clearer action verb. "Convert measurement" is more direct.

**Current Description:**
> "Convert between various measurement units including distance, volume, weight, and temperature"

**Proposed Description:**
> "Convert measurements between different units. Supports distance (miles, kilometers, feet, meters, inches, etc.), volume (gallons, liters, cups, tablespoons, etc.), weight/mass (pounds, kilograms, ounces, grams, etc.), and temperature (Fahrenheit, Celsius, Kelvin). Use this for unit conversions like 'how many cups in a gallon' or 'convert 5 miles to kilometers'. Do NOT use for calculations without unit conversions (use calculate) or currency conversions."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "value", 
        "float", 
        required=False, 
        default=1.0,
        description="The numeric value to convert. If not explicitly provided in the question, defaults to 1.0 (e.g., 'how many cups in a gallon' means convert 1 gallon)"
    ),
    JarvisParameter(
        "from_unit", 
        "string", 
        required=True, 
        description="The source unit to convert FROM. Use the full or common unit name (e.g., 'miles', 'kilometers', 'gallons', 'liters', 'pounds', 'kilograms', 'fahrenheit', 'celsius', 'feet', 'meters', 'cups', 'ounces')"
    ),
    JarvisParameter(
        "to_unit", 
        "string", 
        required=True, 
        description="The target unit to convert TO. Use the full or common unit name (e.g., 'kilometers', 'feet', 'liters', 'cups', 'kilograms', 'grams', 'celsius', 'fahrenheit')"
    ),
    JarvisParameter(
        "category", 
        "string", 
        required=False, 
        description="Optional category hint to disambiguate similar unit names. Values: 'distance', 'volume', 'weight', 'temperature'. Only provide if ambiguous."
    )
]
```

---

### 4. ✅ open_weather_command → **get_weather**
**Current Name:** `open_weather_command`  
**Proposed Name:** `get_weather`  
**Reason:** Simpler, clearer. "Open weather" exposes implementation detail. "Get weather" is the action.

**Current Description:**
> "Gets the current weather or forecast for an optional city or optional date range"

**Proposed Description:**
> "Get current weather conditions or future weather forecast for a specific location. Returns temperature, conditions (sunny, cloudy, rainy, etc.), humidity, wind speed, and precipitation chances. Use this for weather questions like 'how's the weather', 'is it raining', 'what's the temperature', 'will it rain tomorrow', or weather forecasts. Supports current conditions (no datetime provided) or forecasts up to 5 days ahead (with datetime). Do NOT use for: past weather data (not available), climate statistics, or general weather knowledge (use answer_question)."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "city", 
        "string", 
        required=False, 
        default=None,
        description="The city name to get weather for. Use the city name as spoken by the user (e.g., 'Miami', 'New York', 'Tokyo', 'San Francisco'). If not provided, uses the user's default/home location from their profile."
    ),
    JarvisParameter(
        "datetimes", 
        "datetime", 
        required=False, 
        description="Array of ISO datetime strings for weather forecast dates (e.g., ['2025-11-10T00:00:00', '2025-11-11T00:00:00']). Omit this parameter for current weather conditions. Include dates for forecasts. Maximum 5 days in the future."
    ),
    JarvisParameter(
        "unit_system", 
        "string", 
        required=False, 
        default=None,
        description="Temperature unit system: 'metric' (Celsius, km/h) or 'imperial' (Fahrenheit, mph). If not provided, uses user's default preference."
    )
]
```

---

### 5. ✅ read_calendar_command → **get_calendar_events**
**Current Name:** `read_calendar_command`  
**Proposed Name:** `get_calendar_events`  
**Reason:** "Get" is clearer than "read". "Events" is more specific than just "calendar".

**Current Description:**
> "Retrieves calendar events from the user's calendar."

**Proposed Description:**
> "Retrieve the user's calendar events and appointments for specific dates or date ranges. Returns event titles, times, locations, and attendees. Use this for questions like 'what's on my calendar', 'do I have meetings today', 'what's my schedule for tomorrow', or 'show me next week's appointments'. Supports single dates or date ranges. Do NOT use for: creating/adding events, general date/time questions (use answer_question), or scheduling future events (not supported yet)."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "datetimes", 
        "datetime", 
        description="Array of ISO datetime strings for which to retrieve calendar events (e.g., ['2025-11-10T00:00:00'] for a single day, ['2025-11-10T00:00:00', '2025-11-11T00:00:00', '2025-11-12T00:00:00'] for multiple days). If not provided, defaults to today. The time portion is ignored; only the date is used to filter events.",
        required=False, 
        default=None
    )
]
```

---

### 6. ✅ sports_score_command → **get_sports_scores**
**Current Name:** `sports_score_command`  
**Proposed Name:** `get_sports_scores`  
**Reason:** Clearer action verb. Plural "scores" indicates it can return multiple.

**Current Description:** (Already good!)
> "Get sports scores and results for past/completed games. Use this for questions about how teams performed, what scores were, who won/lost, or any results from games that have already happened. Covers NFL, NBA, MLB, NHL, and college sports."

**Proposed Description:**
> "Get sports scores and game results for completed/past games. Returns final scores, winners, game status, and basic game details. Use this for questions about how teams performed, final scores, who won/lost, or results from games that already happened (past or today). Covers NFL, NBA, MLB, NHL, NCAA football, and NCAA basketball. Do NOT use for: upcoming games or schedules (use get_sports_schedule), live/in-progress game updates, player statistics, or team standings."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "team_name", 
        "string", 
        required=True, 
        description="The team name exactly as spoken by the user. Include city/state/school when mentioned (e.g., 'Giants', 'New York Giants', 'Seattle Mariners', 'Carolina Panthers', 'Ohio State Buckeyes', 'Alabama Crimson Tide'). The system will handle disambiguation if multiple teams match."
    ),
    JarvisParameter(
        "datetimes", 
        "array[datetime]", 
        required=False, 
        description="Array of ISO datetime strings for dates to check for scores (e.g., ['2025-11-09T00:00:00'] for a single date, or multiple dates for a range). If not provided, defaults to today. Use past or current dates only, not future dates."
    )
]
```

---

### 7. ✅ sports_schedule_command → **get_sports_schedule**
**Current Name:** `sports_schedule_command`  
**Proposed Name:** `get_sports_schedule`  
**Reason:** Consistency with sports_scores. Singular "schedule" is clearer.

**Current Description:** (Already good!)
> "Get sports schedules and upcoming games. Use this for questions about future games, when teams play next, upcoming matchups, or game times. This is for FUTURE events only, not past results. Covers NFL, NBA, MLB, NHL, and college sports."

**Proposed Description:**
> "Get upcoming sports game schedules and future matchups. Returns game times, opponents, venues, and broadcast information for games that have not yet been played. Use this for questions about when teams play next, upcoming games, future matchups, or game schedules. This is for FUTURE events only (today or later). Covers NFL, NBA, MLB, NHL, NCAA football, and NCAA basketball. Do NOT use for: past game results or scores (use get_sports_scores), live game updates, or games that already finished."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "team_name", 
        "string", 
        required=True, 
        description="The team name exactly as spoken by the user. Include city/state/school when mentioned (e.g., 'Giants', 'New York Giants', 'Seattle Mariners', 'Carolina Panthers', 'Ohio State Buckeyes', 'Alabama Crimson Tide'). The system will handle disambiguation if multiple teams match."
    ),
    JarvisParameter(
        "datetimes", 
        "datetime", 
        required=True, 
        description="Array of ISO datetime strings for dates to check for upcoming games (e.g., ['2025-11-10T00:00:00'] for tomorrow, or a range of dates). If no specific date mentioned in the question, use today's date. For relative dates like 'this weekend' or 'next week', convert to actual ISO datetimes."
    )
]
```

---

### 8. ✅ tell_a_story → **tell_story**
**Current Name:** `tell_a_story`  
**Proposed Name:** `tell_story`  
**Reason:** Consistency - remove "a" for cleaner naming (tell_story vs tell_a_story)

**Current Description:**
> "Writes and reads a story to the user in chunks. Use 'continue' to hear more, 'end story' to finish."

**Proposed Description:**
> "Create and narrate an original story for the user in manageable chunks. Stories are generated based on the user's chosen subject/theme and target audience age. The story is delivered in parts, allowing the user to pace the narration. Use this when user requests a story, bedtime story, tale, or narrative. Supports: starting new stories, continuing existing stories, and ending stories. Do NOT use for: reading existing books, summarizing known stories, or telling jokes (use tell_joke)."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "story_subject", 
        "string", 
        required=False, 
        description="The subject, theme, or topic for the story (e.g., 'a brave knight', 'space adventure', 'friendly dinosaur', 'princess and dragon'). If not provided, a random age-appropriate theme will be chosen."
    ),
    JarvisParameter(
        "target_audience_age", 
        "int", 
        required=False, 
        default=5,
        description="The target age of the listener in years, used to adjust story complexity and content appropriateness (e.g., 3, 5, 8, 12). Defaults to 5 if not specified."
    ),
    JarvisParameter(
        "word_count", 
        "int", 
        required=False, 
        default=750,
        description="Target total word count for the complete story (e.g., 500, 750, 1000). The story will be delivered in chunks. Defaults to 750 words."
    ),
    JarvisParameter(
        "action", 
        "string", 
        required=False, 
        default="start",
        description="The story action to perform: 'start' (begin a new story), 'continue' (hear the next part of an ongoing story), or 'end' (finish and summarize the current story). Defaults to 'start'."
    ),
    JarvisParameter(
        "session_id", 
        "string", 
        required=False, 
        description="Story session identifier for continuing or ending an existing story. Only required when action is 'continue' or 'end'. Do not provide for new stories (action='start')."
    )
]
```

---

### 9. ✅ tell_a_joke → **tell_joke**
**Current Name:** `tell_a_joke`  
**Proposed Name:** `tell_joke`  
**Reason:** Consistency - remove "a" for cleaner naming

**Current Description:**
> "Tells a family-friendly joke, optionally on a specific topic"

**Proposed Description:**
> "Tell a family-friendly, clean joke to the user. Jokes are appropriate for all ages and can optionally be focused on a specific topic or subject. Use this when user asks for a joke, asks you to make them laugh, or requests humor. Do NOT use for: stories (use tell_story), riddles (use answer_question), or humor that requires current events (use web_search_command for current event humor)."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "topic", 
        "string", 
        required=False, 
        default=None,
        description="Optional topic or subject for the joke (e.g., 'programming', 'animals', 'food', 'science', 'knock-knock'). If provided, the joke will be related to this topic. If omitted, a random family-friendly joke will be told."
    )
]
```

---

### 10. ✅ web_search_command → **search_web**
**Current Name:** `web_search_command`  
**Proposed Name:** `search_web`  
**Reason:** Clearer action verb. "Search web" is more direct.

**Current Description:** (Already excellent!)
> "Performs live web searches for current information, recent events, real-time data, and questions requiring up-to-date answers. Use this for current events, breaking news, live results, or anything that might have changed recently and requires an internet search."

**Proposed Description:**
> "Search the web for current, real-time, or recently updated information. Returns relevant search results from the internet for questions that require up-to-date answers. Use this for: current events, breaking news, recent happenings, live data, trending topics, latest updates, recent changes, or anything that might have changed in the last few days/weeks. Do NOT use for: established facts/history (use answer_question), weather (use get_weather), sports scores/schedules (use sports commands), calculations (use calculate), or personal information (use appropriate specific command)."

**Parameter Improvements:**
```python
parameters = [
    JarvisParameter(
        "query", 
        "string", 
        required=True, 
        description="The search query for current or recent information. Should be a clear, specific question or search terms (e.g., 'latest news about artificial intelligence', 'who won the election in Pennsylvania', 'current Tesla stock price', 'when is the next SpaceX launch')"
    )
]
```

---

## Implementation Plan

### Phase 1: Rename Command Files
1. Rename files (keeping old names as comments for reference)
2. Update class names
3. Update imports across codebase

### Phase 2: Update Command Metadata
1. Update `command_name` properties
2. Update `description` properties
3. Update all parameter descriptions

### Phase 3: Test and Validate
1. Run command discovery service
2. Test with LLM to ensure proper command selection
3. Verify parameter extraction accuracy
4. Update test cases with new command names

---

## Summary of Changes

| Current Name | New Name | Main Improvement |
|--------------|----------|------------------|
| `calculator_command` | `calculate` | Clearer action verb, added anti-patterns |
| `general_knowledge_command` | `answer_question` | More intuitive name, better disambiguation |
| `measurement_conversion_command` | `convert_measurement` | Clearer action, more examples |
| `open_weather_command` | `get_weather` | Removes implementation detail, clearer scope |
| `read_calendar_command` | `get_calendar_events` | More specific, better description |
| `sports_score_command` | `get_sports_scores` | Consistent naming, enhanced description |
| `sports_schedule_command` | `get_sports_schedule` | Consistent with scores command |
| `tell_a_story` | `tell_story` | Cleaner naming (remove "a") |
| `tell_a_joke` | `tell_joke` | Cleaner naming (remove "a") |
| `web_search_command` | `search_web` | Clearer action verb |

All descriptions now include:
- ✅ Clear use cases ("Use this for...")
- ✅ Anti-patterns ("Do NOT use for...")
- ✅ Specific scope and limitations
- ✅ Differentiation from similar commands
- ✅ Parameter descriptions with inline examples

