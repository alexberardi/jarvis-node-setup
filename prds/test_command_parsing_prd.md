## Test Command Parsing PRD

### Goals
- Increase consistency of JSON outputs from the LLM.
- Reduce false negatives in test validation without masking real failures.
- Keep changes test-focused unless schema change is explicitly approved.

### Scope
- Update test acceptance logic in `temp/test_command_parsing.py`.
- Document a candidate response schema change for the LLM/tool chain.
- No changes to runtime tool chain until explicitly approved.

---

## Proposed Test Acceptance Changes

### 1) Parse JSON tool calls embedded in `assistant_message`
**Problem:** LLM sometimes returns JSON in `assistant_message` while `tool_calls` is empty.

**Change:**
- If `tool_calls` is empty and `assistant_message` looks like JSON, attempt to parse a `tool_call`.
- Treat parsed `tool_call` as the actual tool call for the test.

**Acceptance:**
- `assistant_message` contains valid JSON with `tool_call` → test uses that tool call.
- If parsing fails, fall back to current behavior.

---

### 2) Allow direct completion only for trivial commands
**Problem:** LLM returns direct answers for non-trivial commands (weather/calendar/sports).

**Change:**
- Define a whitelist of commands allowed to complete without tool calls.
- Suggested: `calculate`, `answer_question`, `tell_joke` (optional).

**Acceptance:**
- If `stop_reason == "complete"` and no tool calls:
  - Pass only if expected command is in whitelist.
  - Otherwise, fail.

---

### 3) Normalize stringified list params
**Problem:** Some tool params arrive as strings that look like lists (e.g., `"['a','b']"`).

**Change:**
- If an actual parameter value is a string that looks like a list, attempt to parse it.
- Compare parsed list with expected list.

**Acceptance:**
- If parse succeeds, compare normalized list values.
- If parse fails, fall back to original string comparison.

---

## Candidate Schema Change (For Approval)

### Proposed Response Format (Strict)
Require a single JSON object output only (no extra text), with a single `tool_call` or `null`.

```json
{
  "message": "brief ack or final reply",
  "tool_call": {
    "name": "<tool_name>",
    "arguments": { "key": "value" }
  }
}
```

Final response:
```json
{
  "message": "final spoken reply",
  "tool_call": null
}
```

### Rationale
- Enforces a single, parseable JSON object.
- Reduces the tendency to embed JSON in plain text.
- Simpler for smaller models and test harnesses.

### Notes
- If this schema change is adopted, tests should **only** accept this JSON shape.
- Any "JSON-in-text" should be treated as failure.

---

## Open Questions
- Confirm the whitelist for direct completion (`calculate`, `answer_question`, `tell_joke`).
- Should we allow direct completion for search queries if a tool is available?
- Do we want to keep `tool_calls` array support, or enforce `tool_call` singular only?
