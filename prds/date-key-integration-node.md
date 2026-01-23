# Date Key Integration - Node Project

## Overview

This PRD describes the changes needed in the Jarvis node project to integrate with the new date key extraction system. The primary change is replacing dynamic date context injection with standardized semantic date keys.

## Background

Previously, the node's `generate_adapter_examples()` function took a `date_context` object and examples referenced specific properties within that context, resulting in hardcoded absolute timestamps in training data.

The new approach:
1. Use standardized semantic keys (`tomorrow`, `next_tuesday`, etc.)
2. Training data contains keys, not timestamps
3. LLM learns to output keys
4. Consumers resolve keys to actual datetimes

## Changes Required

### 1. Create RelativeDateKeys Constants

Replace dynamic date context with a constants class. This class should be **auto-generated** from the LLM proxy API to stay in sync.

#### Auto-Generation Script

Create `scripts/sync_date_keys.py`:

```python
#!/usr/bin/env python3
"""
Sync date keys from jarvis-llm-proxy-api and generate constants file.

Usage:
    python scripts/sync_date_keys.py
    
Or add to CI/CD to run periodically.
"""

import requests
import os
from pathlib import Path

LLM_PROXY_URL = os.getenv("JARVIS_LLM_PROXY_URL", "http://localhost:8000")
OUTPUT_FILE = Path(__file__).parent.parent / "constants" / "relative_date_keys.py"


def fetch_date_keys():
    """Fetch supported date keys from the LLM proxy API."""
    response = requests.get(f"{LLM_PROXY_URL}/v1/adapters/date-keys")
    response.raise_for_status()
    return response.json()


def generate_constants_file(data: dict):
    """Generate Python constants file from API response."""
    keys = data.get("keys", [])
    version = data.get("version", "unknown")
    
    lines = [
        '"""',
        "Auto-generated from jarvis-llm-proxy-api /v1/adapters/date-keys",
        f"Version: {version}",
        "",
        "DO NOT EDIT MANUALLY - Run scripts/sync_date_keys.py to update",
        '"""',
        "",
        "",
        "class RelativeDateKeys:",
        '    """Standardized date key constants for adapter training data."""',
        "",
    ]
    
    for key in sorted(keys):
        const_name = key.upper()
        lines.append(f'    {const_name} = "{key}"')
    
    lines.append("")
    lines.append("")
    lines.append("# List of all keys for iteration")
    lines.append("ALL_DATE_KEYS = [")
    for key in sorted(keys):
        lines.append(f'    RelativeDateKeys.{key.upper()},')
    lines.append("]")
    lines.append("")
    
    return "\n".join(lines)


def main():
    print(f"Fetching date keys from {LLM_PROXY_URL}...")
    data = fetch_date_keys()
    
    print(f"Found {len(data.get('keys', []))} keys")
    
    content = generate_constants_file(data)
    
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(content)
    
    print(f"Generated {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
```

#### Generated Output

Running the script produces `constants/relative_date_keys.py`:

```python
"""
Auto-generated from jarvis-llm-proxy-api /v1/adapters/date-keys
Version: 1.0

DO NOT EDIT MANUALLY - Run scripts/sync_date_keys.py to update
"""


class RelativeDateKeys:
    """Standardized date key constants for adapter training data."""

    AFTERNOON = "afternoon"
    AT_10AM = "at_10am"
    AT_10PM = "at_10pm"
    AT_11AM = "at_11am"
    # ... etc
    DAY_AFTER_TOMORROW = "day_after_tomorrow"
    DAY_BEFORE_YESTERDAY = "day_before_yesterday"
    EVENING = "evening"
    LAST_FRIDAY = "last_friday"
    LAST_MONDAY = "last_monday"
    # ... etc
    MORNING = "morning"
    NEXT_FRIDAY = "next_friday"
    NEXT_MONDAY = "next_monday"
    # ... etc
    NEXT_WEEK = "next_week"
    NEXT_WEEKEND = "next_weekend"
    NIGHT = "night"
    NOON = "noon"
    THIS_MONTH = "this_month"
    THIS_WEEK = "this_week"
    THIS_WEEKEND = "this_weekend"
    TODAY = "today"
    TOMORROW = "tomorrow"
    YESTERDAY = "yesterday"


# List of all keys for iteration
ALL_DATE_KEYS = [
    RelativeDateKeys.AFTERNOON,
    RelativeDateKeys.AT_10AM,
    # ... etc
]
```

### 2. Update generate_adapter_examples()

#### Before (current implementation)

```python
def generate_adapter_examples(date_context: dict) -> list:
    return [
        {
            "voice_command": "What's the weather tomorrow?",
            "expected_tool_call": {
                "name": "get_weather",
                "arguments": {
                    "resolved_datetimes": [date_context["relative_dates"]["tomorrow"]["utc_start_of_day"]]
                }
            }
        },
        # ...
    ]
```

#### After (new implementation)

```python
from constants.relative_date_keys import RelativeDateKeys

def generate_adapter_examples() -> list:
    """
    Generate training examples using semantic date keys.
    
    Note: No date_context parameter needed - we use standardized keys.
    """
    return [
        {
            "voice_command": "What's the weather tomorrow?",
            "expected_tool_call": {
                "name": "get_weather",
                "arguments": {
                    "resolved_datetimes": [RelativeDateKeys.TOMORROW]
                }
            }
        },
        {
            "voice_command": "What's the weather tomorrow morning?",
            "expected_tool_call": {
                "name": "get_weather",
                "arguments": {
                    "resolved_datetimes": [RelativeDateKeys.TOMORROW_MORNING]
                }
            }
        },
        {
            "voice_command": "Show my calendar this weekend",
            "expected_tool_call": {
                "name": "get_calendar_events",
                "arguments": {
                    "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]
                }
            }
        },
        {
            "voice_command": "How did the Giants do last Saturday?",
            "expected_tool_call": {
                "name": "get_sports_scores",
                "arguments": {
                    "team_name": "Giants",
                    "resolved_datetimes": [RelativeDateKeys.LAST_SATURDAY]
                }
            }
        },
        # ...
    ]
```

### 3. Handle Implicit Dates

For commands where date is implied (e.g., "What's the weather?" meaning today):

**Option A: Include explicit "today" key**
```python
{
    "voice_command": "What's the weather?",
    "expected_tool_call": {
        "name": "get_weather",
        "arguments": {
            "resolved_datetimes": [RelativeDateKeys.TODAY]
        }
    }
}
```

**Option B: Omit datetime param, let tool default**
```python
{
    "voice_command": "What's the weather?",
    "expected_tool_call": {
        "name": "get_weather",
        "arguments": {}  # Tool defaults to today
    }
}
```

This is **command-specific** - choose based on the tool's behavior and natural language patterns for that command.

### 4. Standalone vs Decomposed Keys

**Use standalone combined keys** for common phrases with tomorrow/yesterday/tonight:

```python
{
    "voice_command": "What's the weather tomorrow night?",
    "expected_tool_call": {
        "name": "get_weather",
        "arguments": {
            "resolved_datetimes": [RelativeDateKeys.TOMORROW_NIGHT]
        }
    }
}
```

Available standalone keys:
- `TONIGHT`, `LAST_NIGHT`, `TOMORROW_NIGHT`
- `TOMORROW_MORNING`, `TOMORROW_AFTERNOON`, `TOMORROW_EVENING`
- `YESTERDAY_MORNING`, `YESTERDAY_AFTERNOON`, `YESTERDAY_EVENING`

**Use decomposed keys** for weekday + time combinations:

```python
{
    "voice_command": "Forecast for next Tuesday morning",
    "expected_tool_call": {
        "name": "get_weather",
        "arguments": {
            "city": None,  # not specified
            "resolved_datetimes": [
                RelativeDateKeys.NEXT_TUESDAY,
                RelativeDateKeys.MORNING
            ]
        }
    }
}
```

The consumer (jcc) can:
- Combine them: `next_tuesday` + `morning` → `2026-01-28T07:00:00Z`
- Use just the date if time isn't relevant
- Handle however makes sense for the tool

### 5. Multi-Day Periods

For periods spanning multiple days:

```python
{
    "voice_command": "What's on my calendar next week?",
    "expected_tool_call": {
        "name": "get_calendar_events",
        "arguments": {
            "resolved_datetimes": [RelativeDateKeys.NEXT_WEEK]
        }
    }
}
```

The consumer expands `next_week` into the actual date range based on their context.

### 6. Testing Updates

Update tests to use the new constants:

```python
from constants.relative_date_keys import RelativeDateKeys, ALL_DATE_KEYS

def test_all_date_keys_are_valid():
    """Ensure all keys are lowercase with underscores."""
    for key in ALL_DATE_KEYS:
        assert key == key.lower()
        assert " " not in key


def test_generate_adapter_examples_uses_keys():
    """Ensure examples use RelativeDateKeys, not raw strings."""
    examples = generate_adapter_examples()
    
    for example in examples:
        tool_call = example.get("expected_tool_call", {})
        args = tool_call.get("arguments", {})
        
        if "resolved_datetimes" in args:
            for dt in args["resolved_datetimes"]:
                assert dt in ALL_DATE_KEYS, f"Unknown date key: {dt}"
```

## Migration Checklist

- [ ] Create `scripts/sync_date_keys.py`
- [ ] Run script to generate `constants/relative_date_keys.py`
- [ ] Update `generate_adapter_examples()` to remove date_context parameter
- [ ] Update all example definitions to use `RelativeDateKeys` constants
- [ ] Update any code that calls `generate_adapter_examples()` to not pass date_context
- [ ] Update tests
- [ ] Add sync script to CI/CD or document when to run it

## Keeping In Sync

Run the sync script when:
1. The LLM proxy adds new date keys
2. Before generating new training data
3. As part of release preparation

Consider adding to CI:
```yaml
- name: Sync date keys
  run: python scripts/sync_date_keys.py
  
- name: Check for uncommitted changes
  run: git diff --exit-code constants/relative_date_keys.py
```

This ensures the constants file is always up-to-date with the API.

## API Reference

The source of truth for date keys:

```
GET http://llm-proxy:8000/v1/adapters/date-keys
```

This endpoint is **unauthenticated** and returns the complete vocabulary of supported keys.