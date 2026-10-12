#!/usr/bin/env python3
"""
Timezone command for Jarvis.
Returns the current time in a specified location/timezone.
"""

import re
from typing import Any, List
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from jarvis_command_sdk import (
    CommandAntipattern,
    CommandExample,
    FastPathPattern,
    IJarvisCommand,
    PreRouteResult,
)
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


# Date questions answered without the LLM. The fast path used to cover only "what day is it" / "what's the date".
# Everything else ("what day is it tomorrow", "what year is it", "how many days until Christmas") fell through to the
# model, which answered with the time of day or invented a date.
_DATE_FAST_PATH_RE = (
    r"^\s*(?:what(?:'?s|\s+is)?\s+(?:the\s+)?(?:date|day)(?:\s+(?:today|tomorrow|yesterday))?"
    r"|what(?:'?s|\s+is)\s+(?:today|tomorrow|yesterday)(?:'?s)?\s+date"
    r"|what\s+day\s+(?:is\s+it|is\s+today|(?:is|are)\s+we)(?:\s+(?:today|tomorrow|yesterday))?"
    r"|what\s+day\s+of\s+the\s+week(?:\s+is\s+it)?(?:\s+(?:today|tomorrow|yesterday))?"
    r"|what\s+(?:year|month)\s+is\s+it(?:\s+now)?"
    r"|what\s+(?:year|month)\s+are\s+we\s+in"
    r"|how\s+many\s+(?:days|sleeps)\s+(?:until|till|to)\s+christmas"
    r"|is\s+it\s+(?:the\s+)?weekend(?:\s+today)?)\s*[?.!]*$"
)


def _date_reply(voice_command: str, now: datetime) -> str | None:
    """Spoken answer for a date question, or None when the request is not one."""
    text = re.sub(r"[^a-z' ]", " ", (voice_command or "").lower())
    text = re.sub(r"\s+", " ", text).strip()
    if not text or not re.match(_DATE_FAST_PATH_RE, text):
        return None

    def fmt(d: datetime) -> str:
        return f"{d.strftime('%A, %B')} {d.day}"

    if re.search(r"\b(?:days|sleeps) (?:until|till|to) christmas\b", text):
        today = now.date()
        xmas = today.replace(month=12, day=25)
        if xmas < today:
            xmas = xmas.replace(year=today.year + 1)
        n = (xmas - today).days
        if n == 0:
            return "Christmas is today."
        if n == 1:
            return "Christmas is tomorrow."
        return f"It's {n} days until Christmas."
    if re.search(r"\bis it (?:the )?weekend\b", text):
        return "Yes, it's the weekend." if now.weekday() >= 5 else f"No, it's {now.strftime('%A')}."
    if re.search(r"\bwhat year\b", text):
        return f"It's {now.year}."
    if re.search(r"\bwhat month\b", text):
        return f"It's {now.strftime('%B')}."
    if re.search(r"\bwhat(?:'s| is)? (?:the )?(?:date|day)\b|\bwhat day\b|\bwhat(?:'s| is) (?:today|tomorrow|yesterday)", text):
        if "tomorrow" in text:
            return f"Tomorrow is {fmt(now + timedelta(days=1))}."
        if "yesterday" in text:
            return f"Yesterday was {fmt(now - timedelta(days=1))}."
        return f"It's {fmt(now)}."
    return None


class TimezoneCommand(IJarvisCommand):
    """Command for getting the current time in a location"""

    @property
    def command_name(self) -> str:
        return "get_current_time"

    @property
    def description(self) -> str:
        return "Current time/date, locally or in a city/state/country. Use for 'what time is it?', 'what day is it?', 'what time is it in Tokyo?'. Not for weather — use get_weather."

    @property
    def allow_direct_answer(self) -> bool:
        return True

    @property
    def keywords(self) -> List[str]:
        return [
            "time", "current time", "what time", "time zone", "timezone",
            "clock", "time in", "what day", "date", "today"
        ]

    @property
    def parameters(self) -> List[JarvisParameter]:
        return [
            JarvisParameter(
                "location",
                "string",
                required=False,
                description="City, state, or country name (e.g., 'Tokyo', 'California', 'London'). Omit for local time."
            )
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def critical_rules(self) -> List[str]:
        return [
            "TIME queries only, not weather. Extract location name only.",
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

    # Generic/placeholder values the LLM might pass instead of omitting
    _LOCAL_PLACEHOLDERS = {"here", "local", "current", "now", "default", "none", "null", "my location", ""}

    # ------------------------------------------------------------------
    # Fast-path patterns — deterministic regex bypasses the LLM entirely.
    # The SDK's default pre_route() iterates these in order; first match wins.
    # ------------------------------------------------------------------
    @property
    def fast_path_patterns(self) -> List[FastPathPattern]:
        return [
            # Order matters: try "time in X" first so "what time is it in Tokyo"
            # doesn't get caught by the local-time pattern.
            FastPathPattern(
                id="get_current_time.in_location",
                description="Bypass LLM for 'what time is it in <city>' / 'time in <country>'",
                example="what time is it in Tokyo",
                regex=r"\b(?:what(?:'?s|\s+is)?\s+the\s+time|what\s+time\s+is\s+it|current\s+time|time)\s+in\s+(?P<location>[a-zA-Z][a-zA-Z\s]+?)\s*[?.!]*$",
                handler="_fp_time_in_location",
            ),
            FastPathPattern(
                id="get_current_time.local",
                description="Bypass LLM for local time queries ('what time is it', 'current time')",
                example="what time is it",
                regex=r"^\s*(?:what(?:'?s|\s+is)?\s+the\s+time|what\s+time\s+is\s+it|current\s+time|the\s+time)\s*[?.!]*$",
                handler="_fp_time_local",
            ),
            FastPathPattern(
                id="get_current_time.local_date",
                description="Bypass LLM for local date/day queries ('what day is it', 'what's today's date')",
                example="what day is it",
                regex=_DATE_FAST_PATH_RE,
                handler="_fp_date_local",
            ),
        ]

    def _fp_time_in_location(self, match, voice_command: str) -> PreRouteResult | None:
        location = match.group("location").strip()
        if not location or location.lower() in self._LOCAL_PLACEHOLDERS:
            return PreRouteResult(arguments={})
        return PreRouteResult(arguments={"location": location})

    def _fp_time_local(self, match, voice_command: str) -> PreRouteResult | None:
        return PreRouteResult(arguments={})

    def _fp_date_local(self, match, voice_command: str) -> PreRouteResult | None:
        # The local-time pattern composes its message in run(); date queries
        # need a different shape ("It's Monday, January 28." vs "It's 3:45 PM.")
        # so we override the spoken response here and let run() still compute
        # structured fields for any downstream consumer.
        now = datetime.now().astimezone()
        reply = _date_reply(voice_command, now)
        if reply is None:
            date_str = f"{now.strftime('%A, %B')} {now.day}"
            reply = f"It's {date_str}."
        return PreRouteResult(arguments={}, spoken_response=reply)

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        """Execute the timezone command"""
        try:
            location = (kwargs.get("location") or "").strip()
            location_lower = location.lower()

            # No location or generic placeholder → return local time
            if not location or location_lower in self._LOCAL_PLACEHOLDERS:
                current_time = datetime.now().astimezone()
                timezone_str = str(current_time.tzinfo)
                location = "local"
            else:
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

                tz = ZoneInfo(timezone_str)
                current_time = datetime.now(tz)

            # Format time for display
            time_str = current_time.strftime("%I:%M %p").lstrip("0")  # "3:45 PM" / "11:05 AM"
            date_str = current_time.strftime("%A, %B %d")  # e.g., "Monday, January 28"

            context_data: dict[str, Any] = {
                "location": location,
                "timezone": timezone_str,
                "current_time": time_str,
                "current_date": date_str,
                "iso_datetime": current_time.isoformat(),
            }

            # Always pre-compose the spoken sentence so the command-center
            # voice fast-path can speak it directly (skipping the formatting
            # LLM, which otherwise risks generic filler like "Task completed.").
            # Keep it terse — the user asked for the time, not the calendar
            # date. Trimming the day/month off the spoken response saves
            # ~3s of TTS playback per query.
            _date = _date_reply(getattr(request_info, "voice_command", ""), current_time) if location == "local" else None
            if _date:
                context_data["message"] = _date
            elif location == "local":
                context_data["message"] = f"It's {time_str}."
            else:
                context_data["message"] = (
                    f"It's {time_str} in {location.title()}."
                )

            return CommandResponse.follow_up_response(context_data=context_data)

        except Exception as e:
            return CommandResponse.error_response(
                error_details=f"Timezone lookup error: {str(e)}",
                context_data={
                    "location": kwargs.get("location"),
                    "error": str(e)
                }
            )
