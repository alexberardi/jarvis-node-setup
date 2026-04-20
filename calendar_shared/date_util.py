import time
from datetime import datetime, timedelta
from typing import Optional

try:
    from jarvis_log_client import JarvisLogger
except ImportError:
    import logging

    class JarvisLogger:
        def __init__(self, **kw): self._log = logging.getLogger(kw.get("service", __name__))
        def info(self, msg, **kw): self._log.info(msg)
        def warning(self, msg, **kw): self._log.warning(msg)
        def error(self, msg, **kw): self._log.error(msg)
        def debug(self, msg, **kw): self._log.debug(msg)

logger = JarvisLogger(service="jarvis-node")


# ---------------------------------------------------------------------------
# Timezone utilities (inlined from utils/timezone_util.py)
# ---------------------------------------------------------------------------

def get_user_timezone() -> str:
    """
    Detect the user's timezone using system timezone information

    Returns:
        str: Timezone identifier (e.g., 'America/New_York', 'Europe/London')
    """
    try:
        # Get the local timezone offset
        # Note: time.timezone is negative for timezones ahead of UTC, positive for behind UTC
        local_offset = time.timezone if not time.daylight else time.altzone

        # Convert offset to hours
        offset_hours = abs(local_offset) // 3600
        offset_minutes = (abs(local_offset) % 3600) // 60

        # Determine if it's ahead or behind UTC
        # Negative offset means ahead of UTC, positive means behind UTC
        if local_offset < 0:  # Ahead of UTC
            if offset_hours == 0:
                result = "Europe/London"
            elif offset_hours == 1:
                result = "Europe/Paris"
            elif offset_hours == 2:
                result = "Europe/Kiev"
            elif offset_hours == 3:
                result = "Europe/Moscow"
            elif offset_hours == 5:
                result = "Asia/Kolkata"
            elif offset_hours == 8:
                result = "Asia/Shanghai"
            elif offset_hours == 9:
                result = "Asia/Tokyo"
            elif offset_hours == 10:
                result = "Australia/Sydney"
            elif offset_hours == 12:
                result = "Pacific/Auckland"
            else:
                sign = "+"
                result = f"UTC{sign}{offset_hours:02d}:{offset_minutes:02d}"
        else:  # Behind UTC
            if offset_hours == 4:
                result = "America/New_York"  # EDT
            elif offset_hours == 5:
                result = "America/New_York"  # EST
            elif offset_hours == 6:
                result = "America/Chicago"
            elif offset_hours == 7:
                result = "America/Denver"
            elif offset_hours == 8:
                result = "America/Los_Angeles"
            else:
                sign = "-"
                result = f"UTC{sign}{offset_hours:02d}:{offset_minutes:02d}"

        return result

    except Exception as e:
        logger.warning("Timezone detection failed, falling back to UTC", error=str(e))
        return "UTC"


def get_timezone_offset(timezone_name: str) -> int:
    """
    Get the UTC offset in hours for a given timezone name

    Args:
        timezone_name: IANA timezone name (e.g., 'America/New_York')

    Returns:
        int: UTC offset in hours (negative for behind UTC, positive for ahead)
    """
    timezone_offsets = {
        "America/New_York": -5,
        "America/Chicago": -6,
        "America/Denver": -7,
        "America/Los_Angeles": -8,
        "Europe/London": 0,
        "Europe/Paris": 1,
        "Europe/Kiev": 2,
        "Europe/Moscow": 3,
        "Asia/Kolkata": 5,
        "Asia/Shanghai": 8,
        "Asia/Tokyo": 9,
        "Australia/Sydney": 10,
        "Pacific/Auckland": 12,
    }
    return timezone_offsets.get(timezone_name, 0)


# ---------------------------------------------------------------------------
# Date utilities (from utils/date_util.py)
# ---------------------------------------------------------------------------

def get_example_date_with_offset(offset: Optional[int] = 0, user_timezone: str = None) -> str:
    """
    Get an example date string in ISO format with optional offset from current date.
    Returns the UTC representation of the start of day in the user's timezone.

    Args:
        offset: Days offset from current date. Positive adds days, negative subtracts.
               Default is 0 (today).
        user_timezone: The user's timezone (if None, will auto-detect)

    Returns:
        ISO datetime string in UTC format (YYYY-MM-DDTHH:MM:SSZ)

    Examples:
        get_example_date_with_offset()     -> "2025-01-20T05:00:00Z" (today, EST = 5 AM UTC)
        get_example_date_with_offset(0)    -> "2025-01-20T05:00:00Z" (today, EST = 5 AM UTC)
        get_example_date_with_offset(1)    -> "2025-01-21T05:00:00Z" (tomorrow, EST = 5 AM UTC)
        get_example_date_with_offset(-1)   -> "2025-01-19T05:00:00Z" (yesterday, EST = 5 AM UTC)
        get_example_date_with_offset(7)    -> "2025-01-27T05:00:00Z" (next week, EST = 5 AM UTC)
    """
    # Auto-detect user timezone if not provided
    if user_timezone is None:
        user_timezone = get_user_timezone()

    try:
        import pytz

        # Get current time in user's timezone
        user_tz = pytz.timezone(user_timezone)
        now_in_user_tz = datetime.now(user_tz)

        # Calculate target date in user's timezone
        target_date_in_user_tz = now_in_user_tz + timedelta(days=offset)

        # Set to start of day (midnight) in user's timezone
        start_of_day = target_date_in_user_tz.replace(hour=0, minute=0, second=0, microsecond=0)

        # Convert to UTC
        utc_tz = pytz.UTC
        start_of_day_utc = start_of_day.astimezone(utc_tz)

        return start_of_day_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    except ImportError:
        # Fallback if pytz is not available
        logger.warning("pytz not available, using fallback timezone calculation", timezone=user_timezone)

        # Simple offset-based calculation (less accurate but functional)
        offset_hours = get_timezone_offset(user_timezone)

        # Get UTC time and adjust for timezone
        utc_now = datetime.utcnow()
        target_date_utc = utc_now + timedelta(days=offset)

        # Adjust for timezone offset (start of day in user's timezone)
        start_of_day_utc = target_date_utc.replace(hour=abs(offset_hours), minute=0, second=0, microsecond=0)

        return start_of_day_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ical_datetime(datetime_str: str, timezone_info: str = None, target_timezone: str = "America/New_York") -> Optional[datetime]:
    """
    Parse iCal datetime string to Python datetime object with timezone conversion

    Args:
        datetime_str: iCal datetime string (e.g., "20250817T070000")
        timezone_info: Timezone information (e.g., "TZID=America/New_York")
        target_timezone: Target timezone for conversion (default: America/New_York)

    Returns:
        datetime object or None if parsing fails
    """
    if not datetime_str:
        return None

    try:
        # Handle different iCal datetime formats
        if 'T' in datetime_str:
            # Format: 20250817T070000 (YYYYMMDDTHHMMSS)
            if len(datetime_str) == 15:  # YYYYMMDDTHHMMSS
                year = int(datetime_str[0:4])
                month = int(datetime_str[4:6])
                day = int(datetime_str[6:8])
                hour = int(datetime_str[9:11])
                minute = int(datetime_str[11:13])
                second = int(datetime_str[13:15])

                # Create datetime object
                dt = datetime(year, month, day, hour, minute, second)

                # For now, don't do timezone conversion - just return the time as parsed
                # The iCal data already has the correct timezone information
                # TODO: Implement proper timezone conversion using pytz or zoneinfo
                pass

                return dt
            elif len(datetime_str) == 8:  # YYYYMMDD (all-day event)
                year = int(datetime_str[0:4])
                month = int(datetime_str[4:6])
                day = int(datetime_str[6:8])

                return datetime(year, month, day)

        return None

    except Exception as e:
        return None


def parse_date_array(date_array: list) -> list:
    """
    Parse an array of date strings or timestamps into datetime objects

    Args:
        date_array: List of date strings in YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS format

    Returns:
        List of datetime objects, sorted chronologically
    """
    target_dates = []

    if date_array and isinstance(date_array, list):
        for date_str in date_array:
            try:
                # Handle different timestamp formats
                if 'T' in date_str and len(date_str) > 10:
                    # Handle UTC timestamps with 'Z' suffix (YYYY-MM-DDTHH:MM:SSZ)
                    if date_str.endswith('Z'):
                        target_date = datetime.strptime(date_str[:-1], "%Y-%m-%dT%H:%M:%S")
                        # Convert from UTC to local time (assuming Eastern Time for now)
                        # TODO: Make timezone configurable
                        target_date = target_date.replace(tzinfo=None)
                    else:
                        # Handle regular timestamps (YYYY-MM-DDTHH:MM:SS)
                        target_date = datetime.strptime(date_str, "%Y-%m-%dT%H:%M:%S")
                        target_date = target_date.replace(tzinfo=None)
                else:
                    # Parse as date string (YYYY-MM-DD)
                    target_date = datetime.strptime(date_str, "%Y-%m-%d")

                target_dates.append(target_date)
            except ValueError:
                raise ValueError(f"Invalid date format: {date_str}. Please use YYYY-MM-DD, YYYY-MM-DDTHH:MM:SS, or YYYY-MM-DDTHH:MM:SSZ format.")
    else:
        # Default to next 60 days for more useful calendar queries
        today = datetime.now()
        target_dates = [today + timedelta(days=i) for i in range(60)]

    # Sort dates chronologically
    target_dates.sort()
    return target_dates


def format_date_display(target_dates: list) -> str:
    """
    Format a list of dates for display

    Args:
        target_dates: List of datetime objects

    Returns:
        Formatted date string (e.g., "Sunday, August 17" or "August 17 to August 18")
    """
    if len(target_dates) == 1:
        dt = target_dates[0]
        # If it's a specific time (not midnight), include the time
        if dt.hour != 0 or dt.minute != 0:
            return dt.strftime("%A, %B %d at %I:%M %p")
        else:
            return dt.strftime("%A, %B %d")
    else:
        # For multiple dates, show date range
        start_dt = target_dates[0]
        end_dt = target_dates[-1]

        # If both have specific times, include them
        if (start_dt.hour != 0 or start_dt.minute != 0) and (end_dt.hour != 0 or end_dt.minute != 0):
            return f"{start_dt.strftime('%B %d at %I:%M %p')} to {end_dt.strftime('%B %d at %I:%M %p')}"
        else:
            return f"{start_dt.strftime('%B %d')} to {end_dt.strftime('%B %d')}"


def dates_to_strings(target_dates: list) -> list:
    """
    Convert datetime objects to string format for API responses

    Args:
        target_dates: List of datetime objects

    Returns:
        List of date strings in ISO format (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)
    """
    result = []
    for d in target_dates:
        # If it's a specific time (not midnight), include the time
        if d.hour != 0 or d.minute != 0:
            result.append(d.strftime("%Y-%m-%dT%H:%M:%S"))
        else:
            result.append(d.strftime("%Y-%m-%d"))
    return result


def extract_date_from_datetime(datetime_value: str) -> str:
    """
    Extract just the date portion from a datetime string

    Args:
        datetime_value: Datetime string in YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS format

    Returns:
        str: Date string in YYYY-MM-DD format
    """
    if not datetime_value:
        return None

    try:
        # If it's already just a date (YYYY-MM-DD), return as is
        if len(datetime_value) == 10 and datetime_value.count('-') == 2:
            return datetime_value

        # If it's a timestamp (YYYY-MM-DDTHH:MM:SS), extract just the date part
        if 'T' in datetime_value:
            return datetime_value.split('T')[0]

        # Try to parse and extract date
        if len(datetime_value) >= 10:
            return datetime_value[:10]

        return None

    except (ValueError, TypeError, IndexError):
        return None


def extract_dates_from_datetimes(datetime_array: list) -> list:
    """
    Extract date portions from an array of datetime strings

    Args:
        datetime_array: List of datetime strings

    Returns:
        List: Date strings in YYYY-MM-DD format
    """
    if not datetime_array:
        return []

    dates = []
    for dt in datetime_array:
        date = extract_date_from_datetime(dt)
        if date:
            dates.append(date)

    return dates
