#!/usr/bin/env python3
"""
Timezone command for Jarvis.
Returns the current time in a specified location/timezone.
"""

from typing import List
from datetime import datetime
from zoneinfo import ZoneInfo

from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse
from core.request_information import RequestInformation


# Common location-to-timezone mappings
LOCATION_TIMEZONE_MAP = {
    # US States/Regions
    "california": "America/Los_Angeles",
    "los angeles": "America/Los_Angeles",
    "la": "America/Los_Angeles",
    "san francisco": "America/Los_Angeles",
    "new york": "America/New_York",
    "nyc": "America/New_York",
    "ny": "America/New_York",
    "chicago": "America/Chicago",
    "miami": "America/New_York",
    "florida": "America/New_York",
    "texas": "America/Chicago",
    "dallas": "America/Chicago",
    "houston": "America/Chicago",
    "denver": "America/Denver",
    "colorado": "America/Denver",
    "phoenix": "America/Phoenix",
    "arizona": "America/Phoenix",
    "seattle": "America/Los_Angeles",
    "washington": "America/Los_Angeles",
    "boston": "America/New_York",
    "philadelphia": "America/New_York",
    "atlanta": "America/New_York",
    "detroit": "America/Detroit",
    "las vegas": "America/Los_Angeles",
    "hawaii": "Pacific/Honolulu",
    "honolulu": "Pacific/Honolulu",
    "alaska": "America/Anchorage",

    # International Cities
    "london": "Europe/London",
    "uk": "Europe/London",
    "england": "Europe/London",
    "paris": "Europe/Paris",
    "france": "Europe/Paris",
    "berlin": "Europe/Berlin",
    "germany": "Europe/Berlin",
    "tokyo": "Asia/Tokyo",
    "japan": "Asia/Tokyo",
    "sydney": "Australia/Sydney",
    "australia": "Australia/Sydney",
    "melbourne": "Australia/Melbourne",
    "dubai": "Asia/Dubai",
    "uae": "Asia/Dubai",
    "singapore": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong",
    "beijing": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai",
    "china": "Asia/Shanghai",
    "seoul": "Asia/Seoul",
    "korea": "Asia/Seoul",
    "south korea": "Asia/Seoul",
    "mumbai": "Asia/Kolkata",
    "india": "Asia/Kolkata",
    "delhi": "Asia/Kolkata",
    "moscow": "Europe/Moscow",
    "russia": "Europe/Moscow",
    "toronto": "America/Toronto",
    "canada": "America/Toronto",
    "vancouver": "America/Vancouver",
    "mexico city": "America/Mexico_City",
    "mexico": "America/Mexico_City",
    "amsterdam": "Europe/Amsterdam",
    "rome": "Europe/Rome",
    "italy": "Europe/Rome",
    "madrid": "Europe/Madrid",
    "spain": "Europe/Madrid",
    "bangkok": "Asia/Bangkok",
    "thailand": "Asia/Bangkok",
    "cairo": "Africa/Cairo",
    "egypt": "Africa/Cairo",
    "johannesburg": "Africa/Johannesburg",
    "south africa": "Africa/Johannesburg",
    "auckland": "Pacific/Auckland",
    "new zealand": "Pacific/Auckland",
    "brazil": "America/Sao_Paulo",
    "sao paulo": "America/Sao_Paulo",
    "rio": "America/Sao_Paulo",
    "argentina": "America/Argentina/Buenos_Aires",
    "buenos aires": "America/Argentina/Buenos_Aires",
}


class TimezoneCommand(IJarvisCommand):
    """Command for getting the current time in a location"""

    @property
    def command_name(self) -> str:
        return "get_current_time"

    @property
    def description(self) -> str:
        return "Get the current time in a specific city, state, or country. Use for time zone queries like 'what time is it in Tokyo?'"

    @property
    def allow_direct_answer(self) -> bool:
        return True

    @property
    def keywords(self) -> List[str]:
        return [
            "time", "current time", "what time", "time zone", "timezone",
            "clock", "time in"
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "location",
                "string",
                required=True,
                description="City, state, or country name (e.g., 'Tokyo', 'California', 'London')."
            )
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use this command for questions about the current time in a location",
            "This is for TIME queries, not weather queries - 'what time is it in California' vs 'what's the weather in California'",
            "Extract just the location name (city, state, or country) without extra words"
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="get_weather",
                description="Weather conditions, temperature, forecasts. 'What time is it' is NOT weather."
            ),
            CommandAntipattern(
                command_name="search_web",
                description="General web searches, news, current events. Use get_current_time for time queries."
            ),
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for the timezone command"""
        return [
            CommandExample(
                voice_command="What time is it in California?",
                expected_parameters={"location": "California"},
                is_primary=True
            ),
            CommandExample(
                voice_command="What time is it in Tokyo?",
                expected_parameters={"location": "Tokyo"}
            ),
            CommandExample(
                voice_command="Current time in London",
                expected_parameters={"location": "London"}
            ),
            CommandExample(
                voice_command="What's the time in New York?",
                expected_parameters={"location": "New York"}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Focus areas:
        - "What time is it in [location]?" - primary pattern
        - Various locations (US states, international cities)
        - Casual phrasings
        """
        examples = [
            # === PRIMARY PATTERN: "What time is it in [location]?" ===
            CommandExample(voice_command="What time is it in California?", expected_parameters={"location": "California"}, is_primary=True),
            CommandExample(voice_command="What time is it in Tokyo?", expected_parameters={"location": "Tokyo"}, is_primary=False),
            CommandExample(voice_command="What time is it in New York?", expected_parameters={"location": "New York"}, is_primary=False),
            CommandExample(voice_command="What time is it in London?", expected_parameters={"location": "London"}, is_primary=False),
            CommandExample(voice_command="What time is it in Los Angeles?", expected_parameters={"location": "Los Angeles"}, is_primary=False),
            CommandExample(voice_command="What time is it in Chicago?", expected_parameters={"location": "Chicago"}, is_primary=False),
            CommandExample(voice_command="What time is it in Miami?", expected_parameters={"location": "Miami"}, is_primary=False),
            CommandExample(voice_command="What time is it in Paris?", expected_parameters={"location": "Paris"}, is_primary=False),
            CommandExample(voice_command="What time is it in Sydney?", expected_parameters={"location": "Sydney"}, is_primary=False),
            CommandExample(voice_command="What time is it in Dubai?", expected_parameters={"location": "Dubai"}, is_primary=False),

            # === "Current time in [location]" ===
            CommandExample(voice_command="Current time in Berlin", expected_parameters={"location": "Berlin"}, is_primary=False),
            CommandExample(voice_command="Current time in Singapore", expected_parameters={"location": "Singapore"}, is_primary=False),

            # === "What's the time in [location]?" ===
            CommandExample(voice_command="What's the time in Hong Kong?", expected_parameters={"location": "Hong Kong"}, is_primary=False),
            CommandExample(voice_command="What's the time in Seattle?", expected_parameters={"location": "Seattle"}, is_primary=False),

            # === "Time in [location]" (abbreviated) ===
            CommandExample(voice_command="Time in Tokyo", expected_parameters={"location": "Tokyo"}, is_primary=False),
            CommandExample(voice_command="Time in India", expected_parameters={"location": "India"}, is_primary=False),

            # === Country names ===
            CommandExample(voice_command="What time is it in Japan?", expected_parameters={"location": "Japan"}, is_primary=False),
            CommandExample(voice_command="What time is it in Australia?", expected_parameters={"location": "Australia"}, is_primary=False),
            CommandExample(voice_command="What time is it in Germany?", expected_parameters={"location": "Germany"}, is_primary=False),
        ]
        return examples

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the timezone command"""
        try:
            location = kwargs.get("location", "").strip()

            if not location:
                return CommandResponse.error_response(
                    error_details="Location parameter is required",
                    context_data={
                        "error": "Missing location"
                    }
                )

            # Normalize location for lookup
            location_lower = location.lower()

            # Find timezone for location
            timezone_str = LOCATION_TIMEZONE_MAP.get(location_lower)

            if not timezone_str:
                # Try partial matching
                for loc_key, tz in LOCATION_TIMEZONE_MAP.items():
                    if loc_key in location_lower or location_lower in loc_key:
                        timezone_str = tz
                        break

            if not timezone_str:
                return CommandResponse.error_response(
                    error_details=f"Unknown location: {location}",
                    context_data={
                        "location": location,
                        "error": "Location not found in timezone database"
                    }
                )

            # Get current time in that timezone
            tz = ZoneInfo(timezone_str)
            current_time = datetime.now(tz)

            # Format time for display
            time_str = current_time.strftime("%I:%M %p")  # e.g., "3:45 PM"
            date_str = current_time.strftime("%A, %B %d")  # e.g., "Monday, January 28"

            return CommandResponse.follow_up_response(
                context_data={
                    "location": location,
                    "timezone": timezone_str,
                    "current_time": time_str,
                    "current_date": date_str,
                    "iso_datetime": current_time.isoformat()
                }
            )

        except Exception as e:
            return CommandResponse.error_response(
                error_details=f"Timezone lookup error: {str(e)}",
                context_data={
                    "location": kwargs.get("location"),
                    "error": str(e)
                }
            )
