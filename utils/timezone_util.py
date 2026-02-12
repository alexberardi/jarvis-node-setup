import time


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
        
        # print(f"🔍 Timezone detection: local_offset={local_offset}, offset_hours={offset_hours}, offset_minutes={offset_minutes}")
        
        # Determine if it's ahead or behind UTC
        # Negative offset means ahead of UTC, positive means behind UTC
        if local_offset < 0:  # Ahead of UTC
            if offset_hours == 0:
                result = "Europe/London"     # GMT/BST
            elif offset_hours == 1:
                result = "Europe/Paris"      # CET/CEST
            elif offset_hours == 2:
                result = "Europe/Kiev"       # EET/EEST
            elif offset_hours == 3:
                result = "Europe/Moscow"     # MSK
            elif offset_hours == 5:
                result = "Asia/Kolkata"      # IST
            elif offset_hours == 8:
                result = "Asia/Shanghai"     # CST
            elif offset_hours == 9:
                result = "Asia/Tokyo"        # JST
            elif offset_hours == 10:
                result = "Australia/Sydney"  # AEST/AEDT
            elif offset_hours == 12:
                result = "Pacific/Auckland"  # NZST/NZDT
            else:
                # Fallback: return offset-based timezone
                sign = "+"
                result = f"UTC{sign}{offset_hours:02d}:{offset_minutes:02d}"
        else:  # Behind UTC
            if offset_hours == 4:
                result = "America/New_York"  # EDT
            elif offset_hours == 5:
                result = "America/New_York"  # EST
            elif offset_hours == 6:
                result = "America/Chicago"   # CST/CDT
            elif offset_hours == 7:
                result = "America/Denver"    # MST/MDT
            elif offset_hours == 8:
                result = "America/Los_Angeles"  # PST/PDT
            else:
                # Fallback: return offset-based timezone
                sign = "-"
                result = f"UTC{sign}{offset_hours:02d}:{offset_minutes:02d}"
        
        # print(f"🔍 Timezone detection result: {result}")
        return result
        
    except Exception as e:
        print(f"⚠️  Timezone detection failed: {e}, falling back to UTC")
        # Fallback to UTC if detection fails
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
        # US Timezones
        "America/New_York": -5,      # EST (UTC-5), EDT (UTC-4)
        "America/Chicago": -6,       # CST (UTC-6), CDT (UTC-5)
        "America/Denver": -7,        # MST (UTC-7), MDT (UTC-6)
        "America/Los_Angeles": -8,   # PST (UTC-8), PDT (UTC-7)
        
        # European Timezones
        "Europe/London": 0,          # GMT (UTC+0), BST (UTC+1)
        "Europe/Paris": 1,           # CET (UTC+1), CEST (UTC+2)
        "Europe/Kiev": 2,            # EET (UTC+2), EEST (UTC+3)
        "Europe/Moscow": 3,          # MSK (UTC+3)
        
        # Asian Timezones
        "Asia/Kolkata": 5,           # IST (UTC+5)
        "Asia/Shanghai": 8,          # CST (UTC+8)
        "Asia/Tokyo": 9,             # JST (UTC+9)
        
        # Australian/Oceanic Timezones
        "Australia/Sydney": 10,      # AEST (UTC+10), AEDT (UTC+11)
        "Pacific/Auckland": 12,      # NZST (UTC+12), NZDT (UTC+13)
    }
    
    return timezone_offsets.get(timezone_name, 0)


def is_dst_active(timezone_name: str) -> bool:
    """
    Check if daylight saving time is currently active for a timezone
    
    Args:
        timezone_name: IANA timezone name
        
    Returns:
        bool: True if DST is active, False otherwise
    """
    try:
        # Get current system time
        current_time = time.time()
        
        # Check if we're in DST period (rough approximation)
        # This is a simplified check - in production, use proper timezone library
        current_month = time.localtime(current_time).tm_mon
        
        # DST typically runs from March to November in most regions
        dst_months = [3, 4, 5, 6, 7, 8, 9, 10, 11]
        
        return current_month in dst_months
        
    except (OSError, OverflowError):
        return False


def get_current_timezone_offset() -> int:
    """
    Get the current system timezone offset in hours
    
    Returns:
        int: Current UTC offset in hours (negative for behind UTC, positive for ahead)
    """
    try:
        local_offset = time.timezone if not time.daylight else time.altzone
        return -(local_offset // 3600)  # Convert to hours and flip sign for intuitive use
    except (OSError, AttributeError):
        return 0


def convert_utc_to_local(utc_datetime_str: str, fallback_to_utc: bool = True):
    """
    Convert a UTC datetime string to the user's local timezone
    Note: ESPN API sometimes returns times that are already in local timezone despite the 'Z' suffix
    
    Args:
        utc_datetime_str: UTC datetime string (e.g., "2025-08-19T18:20Z")
        fallback_to_utc: If True, return UTC time if conversion fails; if False, return None
        
    Returns:
        datetime: Local datetime object, or UTC datetime if conversion fails and fallback_to_utc is True, or None if fallback_to_utc is False
    """
    try:
        # Parse time string (handle both "Z" and "+00:00" formats)
        if utc_datetime_str.endswith('Z'):
            time_str = utc_datetime_str.replace('Z', '+00:00')
        else:
            time_str = utc_datetime_str
        
        from datetime import datetime
        parsed_time = datetime.fromisoformat(time_str)
        
        # Always convert UTC times to local timezone - don't make assumptions about what's already local
        user_tz = get_user_timezone()
        # print(f"🔍 Converting UTC time {parsed_time} to timezone: {user_tz}")
        
        try:
            import pytz
            utc_tz = pytz.UTC
            local_tz = pytz.timezone(user_tz)
            
            # Check if the datetime already has timezone info
            if parsed_time.tzinfo is not None:
                # If it's already timezone-aware, just convert to local time
                local_time = parsed_time.astimezone(local_tz)
                # print(f"🔍 Conversion successful: {parsed_time} → {local_time} {user_tz}")
                return local_time
            else:
                # If it's naive, localize it first then convert
                utc_aware = utc_tz.localize(parsed_time)
                local_time = utc_aware.astimezone(local_tz)
                # print(f"🔍 Conversion successful: {parsed_time} UTC → {local_time} {user_tz}")
                return local_time
            
        except Exception as tz_error:
            print(f"⚠️  Timezone conversion failed: {tz_error}")
            # If pytz is not available or timezone conversion fails
            if fallback_to_utc:
                return parsed_time
            else:
                return None
                
    except Exception as parse_error:
        print(f"⚠️  Date parsing failed: {parse_error}")
        # If datetime parsing fails
        if fallback_to_utc:
            return None
        else:
            return None


def format_datetime_local(datetime_obj, format_str: str = "%Y-%m-%d %I:%M %p") -> str:
    """
    Format a datetime object in the user's local timezone
    
    Args:
        datetime_obj: datetime object (can be UTC or local)
        format_str: Format string for the output
        
    Returns:
        str: Formatted datetime string in local timezone
    """
    try:
        if datetime_obj is None:
            return "TBD"
        
        # If it's already timezone-aware, convert to local
        if datetime_obj.tzinfo is not None:
            user_tz = get_user_timezone()
            try:
                import pytz
                local_tz = pytz.timezone(user_tz)
                local_time = datetime_obj.astimezone(local_tz)
                return local_time.strftime(format_str)
            except Exception as tz_error:
                print(f"⚠️  Timezone conversion failed in format_datetime_local: {tz_error}")
                # Fallback to UTC formatting
                return datetime_obj.strftime(format_str)
        else:
            # Assume it's already local time
            return datetime_obj.strftime(format_str)
            
    except Exception as e:
        print(f"⚠️  Error in format_datetime_local: {e}")
        return "TBD"
