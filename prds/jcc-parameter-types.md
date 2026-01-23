# Parameter Types, Validation, and Tool Schema Updates

This PRD documents the parameter type system used by Jarvis Command Center, how the
server validates tool-call parameters, and what clients must change to stay compatible.

## Goals
- Define a compact, typed parameter system (including arrays) for tool parameters.
- Map parameter types to JSON tool schema (`format: date-time`, etc.).
- Validate LLM tool-call parameters and retry when types are invalid.
- Avoid auto-injecting dates; only enforce correct types/format.

## Non-Goals
- Nested arrays (array of arrays) are not supported.
- Automatic parameter correction (server-side mutation) is not performed.

## Type Grammar (Single Depth)
Supported scalar types:
- `string`
- `int`
- `float`
- `bool`
- `date`
- `datetime`

Supported array types:
- `array<string>`
- `array<int>`
- `array<float>`
- `array<bool>`
- `array<date>`
- `array<datetime>`

Aliases accepted:
- `array[datetime]`
- `datetime[]`

## JSON Tool Schema Mapping
The command parameter `type` is converted into JSON tool schema when tools are
registered.

Mapping:
- `string` → `{ "type": "string" }`
- `int` → `{ "type": "integer" }`
- `float` → `{ "type": "number" }`
- `bool` → `{ "type": "boolean" }`
- `date` → `{ "type": "string", "format": "date" }`
- `datetime` → `{ "type": "string", "format": "date-time" }`
- `array<T>` → `{ "type": "array", "items": <schema for T> }`

Example (`resolved_datetimes: array<datetime>`):
```json
{
  "resolved_datetimes": {
    "type": "array",
    "items": { "type": "string", "format": "date-time" }
  }
}
```

## Validation Behavior (Server-Side)
When the LLM returns client tool calls, the server validates parameters using
the declared `CommandParameter.type`.

Rules:
- `date` must be `YYYY-MM-DD`.
- `datetime` must be ISO-8601 with timezone (e.g., `2026-01-18T05:00:00Z`).
- `array<...>` must be a list; every entry must validate.

If any parameter fails validation:
- The server adds a system retry message describing the invalid parameters.
- The LLM is given up to **2 retries** to return correctly typed parameters.
- The server does **not** auto-correct or inject values.

## Relative Dates
Relative phrases (e.g., "tomorrow", "next week") are resolved server-side using
`resolve_relative_date`, and the tool result is provided to the LLM. The LLM is
responsible for using those absolute dates in its final tool call.

## Client Changes Required
1. **Declare parameter types** in `available_commands`:
   - Use `array<datetime>` for date ranges like `resolved_datetimes`.
   - Use `datetime` for single timestamps and `date` for date-only values.
2. **Expect JSON schema updates** in tool registrations:
   - Date/datetime parameters will include `format: "date"` or `format: "date-time"`.
3. **Expect retry behavior** if parameters are malformed:
   - The API may take extra iterations before returning tool calls.

## Example `available_commands` Snippet
```json
{
  "command_name": "get_calendar_events",
  "description": "Read calendar events.",
  "parameters": [
    { "name": "resolved_datetimes", "type": "array<datetime>", "required": false }
  ]
}
```

## Example Tool Call (Correct)
```json
{
  "name": "get_calendar_events",
  "arguments": {
    "resolved_datetimes": ["2026-01-18T05:00:00Z", "2026-01-19T05:00:00Z"]
  }
}
```
