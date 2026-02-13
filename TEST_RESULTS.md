# Command Parsing Test Results

**Date:** 2026-02-13
**Adapter:** `2aa5462873f338bd7f40a0267f665d4e76acd46f96c5c221b56fa12b377840ea`
**Base Model:** `llama-3.1-8b-instruct` (GGUF Q4_K_M + LoRA adapter)
**Training Time:** ~7 minutes (PEFT LoRA, MPS)

## Summary

| Metric | Value |
|--------|-------|
| Total Tests | 72 |
| Passed | 48 |
| Failed | 24 |
| Success Rate | 66.7% |
| Avg Response Time | 2.66s |
| Min Response Time | 1.76s |
| Max Response Time | 30.01s |

## Command Success Rates

| Command | Pass/Total | Rate | Status |
|---------|-----------|------|--------|
| calculate | 8/8 | 100% | Perfect |
| get_calendar_events | 6/6 | 100% | Perfect |
| get_current_time | 1/1 | 100% | Perfect |
| tell_joke | 4/4 | 100% | Perfect |
| set_timer | 14/15 | 93% | Near-perfect |
| get_weather | 5/8 | 62% | Good |
| search_web | 3/5 | 60% | Good |
| answer_question | 2/6 | 33% | Weak |
| get_sports_scores | 5/19 | 26% | Weak |

## Failure Categories

### 1. `get_sports_scores` vs `get_sports_schedule` Confusion (9 failures)

The model frequently routes sports score requests to `get_sports_schedule` instead of `get_sports_scores`. This is the single biggest source of failures.

**Examples:**
- "How did the Giants do?" → `get_sports_schedule` (expected `get_sports_scores`)
- "Did the Celtics win?" → `get_sports_schedule` (expected `get_sports_scores`)
- "How'd the Packers do last night?" → `get_sports_schedule` (expected `get_sports_scores`)

**Root cause:** The model doesn't distinguish between past results (scores) and future games (schedule). Both tools deal with sports teams and dates.

### 2. Missing Default `resolved_datetimes` (6 failures)

When no date is mentioned, the model omits `resolved_datetimes` entirely instead of defaulting to `['today']`.

**Examples:**
- "What's the weather in Miami?" → `{"city": "Miami"}` (missing `resolved_datetimes`)
- "How did the Cowboys do?" → `{"team_name": "Cowboys"}` (missing `resolved_datetimes`)
- "What's the score of the Knicks game?" → `{"team_name": "Knicks"}` (missing `resolved_datetimes`)

**Root cause:** The model treats `resolved_datetimes` as optional when no explicit date reference is in the voice command. The expected behavior is to always include `['today']` as a default.

### 3. Wrong Parameter Names (4 failures)

The `answer_question` command expects `query` but the model sometimes uses `question`.

**Examples:**
- "Who was Albert Einstein?" → `{"question": "Who was Albert Einstein?"}` (expected `query`)
- "What is the capital of France?" → `{"question": "What is the capital of France?"}` (expected `query`)

**Root cause:** The model generates semantically equivalent but syntactically wrong parameter names. The `answer_question` tool schema specifies `query` but `question` is a natural synonym.

### 4. Hallucinated Tools (2 failures)

The model invents tools that don't exist in the available tool set.

**Examples:**
- "Where is Mount Everest?" → `get_location` (doesn't exist, expected `answer_question`)
- "Explain quantum physics" → `explain_concept` (doesn't exist, expected `answer_question`)

**Root cause:** The model generates plausible-sounding tool names instead of mapping to the closest available tool.

### 5. Other Parameter Mismatches (3 failures)

- `unit_system` not extracted from "metric units" (interpreted as location instead)
- "1 hour 30 minutes" → 900s instead of 5400s (parsed 15 minutes instead of 90 minutes)
- "this_year" datetime range → resolved to two dates instead of matching the expected key

## Failed Tests Detail

| # | Description | Expected | Got | Reason |
|---|-------------|----------|-----|--------|
| 1 | Weather in Miami | get_weather (city+date) | get_weather (city only) | Missing resolved_datetimes |
| 5 | Weather in metric units | get_weather (unit_system) | get_weather (location) | Missing unit_system |
| 13 | Capital of France | answer_question (query) | answer_question (question) | Wrong param name |
| 15 | Historical question | answer_question (query) | answer_question (question) | Wrong param name |
| 17 | Geography question | answer_question | get_location | Hallucinated tool |
| 18 | Explain quantum physics | answer_question | explain_concept | Hallucinated tool |
| 19 | Election results search | search_web | get_sports_schedule | Wrong command |
| 22 | Upcoming event search | search_web | get_sports_schedule | Wrong command |
| 23 | Sports championship | get_sports_scores | get_sports_scores | Datetime format mismatch |
| 24 | Current weather | get_weather | get_weather | Missing resolved_datetimes |
| 39 | Giants score | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 40 | Cowboys score | get_sports_scores | get_sports_scores | Missing resolved_datetimes |
| 41 | Carolina Panthers score | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 42 | Knicks score | get_sports_scores | get_sports_scores | Missing resolved_datetimes |
| 43 | Packers last night | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 45 | Ravens last weekend | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 47 | Celtics win? | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 49 | Bruins last night | get_sports_scores | get_sports_scores | Missing resolved_datetimes |
| 50 | Bulls last night | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 52 | Phillies yesterday | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 53 | Did Celtics win? | get_sports_scores | get_sports_schedule | Scores/schedule confusion |
| 54 | Mets score | get_sports_scores | get_sports_scores | Missing resolved_datetimes |
| 55 | Final score Giants | get_sports_scores | None | No response (timeout) |
| 64 | Timer 1h 30m | set_timer (5400s) | set_timer (900s) | Wrong duration |

## Recommendations

1. **Sports scores vs schedule:** Add more discriminating examples in training data. Key signals: "how did X do", "score", "win/lose", "last night" → scores. "When do X play", "next game", "upcoming" → schedule.

2. **Default resolved_datetimes:** Consider injecting `resolved_datetimes: ['today']` in post-processing when the parameter is missing and the tool schema requires it, OR add more training examples where no date is mentioned but `resolved_datetimes` is still present.

3. **Parameter name normalization:** Add `question` → `query` aliasing in the `answer_question` command's parameter validation, or add more training examples with the correct `query` parameter name.

4. **Tool hallucination:** This may improve with more training epochs or a larger/better training dataset with negative examples showing that unknown-sounding requests should map to `answer_question` or `search_web`.

5. **Compound durations:** The "1 hour 30 minutes" → 900s failure suggests the model struggles with multi-unit time parsing. More training examples with compound durations would help.
