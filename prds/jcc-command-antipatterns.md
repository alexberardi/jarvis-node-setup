# Jarvis Command Anti-Patterns (Client Contract)

This PRD documents the **command anti-pattern** structure that clients may send to Jarvis Command Center and how it is used in the system prompt.

## Scope
Anti-patterns help the LLM **avoid common tool-selection mistakes** by describing when *not* to choose a command and which other command to use instead.

## Where It Applies
Anti-patterns are attached to command definitions provided by the client:
- `POST /api/v0/conversation/start` → `available_commands[]`
- Optional pass-through on `client_tools[]` (OpenAI tool format with extra fields)

## Data Model
### CommandAntipattern
```json
{
  "command_name": "search_web",
  "description": "If the user asks for current events or 'this year' results, use search_web instead."
}
```

### CommandDefinition (excerpt)
```json
{
  "command_name": "get_sports_scores",
  "description": "Final scores and results for games already played.",
  "parameters": [ ... ],
  "antipatterns": [
    {
      "command_name": "search_web",
      "description": "Championship or current-event questions should use search_web."
    }
  ]
}
```

## Prompt Placement
Anti-patterns are rendered **inline under each tool** in the system prompt:
```
Tool: get_sports_scores
Description: ...
Anti-patterns:
  - Championship or current-event questions should use search_web.
Parameters:
  - team_name (string) [REQUIRED]: ...
```

## Validation & Filtering
- Anti-patterns are **optional**.
- The server **filters out** anti-patterns whose `command_name` is not in the session’s available commands/tools.
- Malformed entries (missing description or command_name) are ignored.

## Client Guidance
- Keep descriptions short and directive.
- Reference the **command name** that should be used instead.
- Avoid duplicating tool descriptions; focus on *disambiguation*.

## Non-Goals
- This PRD does **not** define tool schemas or server-side tools.
