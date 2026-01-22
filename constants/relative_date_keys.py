"""
Auto-generated from jarvis-llm-proxy-api /v1/adapters/date-keys
Version: placeholder

DO NOT EDIT MANUALLY - Run scripts/sync_date_keys.py to update
"""


class RelativeDateKeys:
    """Standardized date key constants for adapter training data."""

    TODAY = "today"
    TOMORROW = "tomorrow"
    DAY_AFTER_TOMORROW = "day_after_tomorrow"
    YESTERDAY = "yesterday"
    THIS_WEEKEND = "this_weekend"
    NEXT_WEEK = "next_week"
    LAST_WEEKEND = "last_weekend"
    NEXT_MONDAY = "next_monday"
    NEXT_TUESDAY = "next_tuesday"
    NEXT_WEDNESDAY = "next_wednesday"
    NEXT_THURSDAY = "next_thursday"
    NEXT_FRIDAY = "next_friday"
    NEXT_SATURDAY = "next_saturday"
    NEXT_SUNDAY = "next_sunday"
    MORNING = "morning"
    TONIGHT = "tonight"
    LAST_NIGHT = "last_night"
    TOMORROW_NIGHT = "tomorrow_night"
    TOMORROW_MORNING = "tomorrow_morning"
    TOMORROW_AFTERNOON = "tomorrow_afternoon"
    TOMORROW_EVENING = "tomorrow_evening"
    YESTERDAY_MORNING = "yesterday_morning"
    YESTERDAY_AFTERNOON = "yesterday_afternoon"
    YESTERDAY_EVENING = "yesterday_evening"


# List of all keys for iteration
ALL_DATE_KEYS = [
    RelativeDateKeys.DAY_AFTER_TOMORROW,
    RelativeDateKeys.LAST_NIGHT,
    RelativeDateKeys.LAST_WEEKEND,
    RelativeDateKeys.MORNING,
    RelativeDateKeys.NEXT_FRIDAY,
    RelativeDateKeys.NEXT_MONDAY,
    RelativeDateKeys.NEXT_SATURDAY,
    RelativeDateKeys.NEXT_SUNDAY,
    RelativeDateKeys.NEXT_THURSDAY,
    RelativeDateKeys.NEXT_TUESDAY,
    RelativeDateKeys.NEXT_WEEK,
    RelativeDateKeys.NEXT_WEDNESDAY,
    RelativeDateKeys.THIS_WEEKEND,
    RelativeDateKeys.TODAY,
    RelativeDateKeys.TOMORROW,
    RelativeDateKeys.TOMORROW_AFTERNOON,
    RelativeDateKeys.TOMORROW_EVENING,
    RelativeDateKeys.TOMORROW_MORNING,
    RelativeDateKeys.TOMORROW_NIGHT,
    RelativeDateKeys.TONIGHT,
    RelativeDateKeys.YESTERDAY,
    RelativeDateKeys.YESTERDAY_AFTERNOON,
    RelativeDateKeys.YESTERDAY_EVENING,
    RelativeDateKeys.YESTERDAY_MORNING,
]
