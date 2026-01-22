import datetime
import requests
from typing import List, Any, Optional

from constants.relative_date_keys import RelativeDateKeys
from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from scripts.text_to_speech import speak
from services.secret_service import get_secret_value
from utils.date_util import extract_dates_from_datetimes, extract_date_from_datetime


class OpenWeatherCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "get_weather"

    @property
    def keywords(self) -> List[str]:
        return ["weather", "forecast"]

    @property
    def description(self) -> str:
        return "Current weather or up-to-5-day forecast. City is optional (uses default location if omitted). Always include resolved_datetimes (use today for current weather). Not for past weather, climate stats, or general facts."

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise example utterances with expected parameters using date keys"""
        return [
            CommandExample(
                voice_command="What's the weather like?",
                expected_parameters={"resolved_datetimes": [RelativeDateKeys.TODAY]},
                is_primary=True
            ),
            CommandExample(
                voice_command="How's the weather in New York today?",
                expected_parameters={"city": "New York", "resolved_datetimes": [RelativeDateKeys.TODAY]}
            ),
            CommandExample(
                voice_command="What's the forecast for Los Angeles tomorrow?",
                expected_parameters={"city": "Los Angeles", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training"""
        weekend_dates = [RelativeDateKeys.THIS_WEEKEND]
        examples = [
            # Implicit today - no date word (critical pattern to learn)
            # The model MUST infer "today" when no date is mentioned in weather queries
            ("What's the weather like?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, True),
            ("What's the weather in Miami?", {"city": "Miami", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Seattle?", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Denver", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Chicago", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the forecast for Boston?", {"city": "Boston", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Temperature in Phoenix", {"city": "Phoenix", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it raining in Portland?", {"city": "Portland", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it cold in Minneapolis?", {"city": "Minneapolis", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How hot is it in Dallas?", {"city": "Dallas", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            # Additional implicit today examples - varied phrasings with city, no date word
            ("Tell me the weather in Atlanta", {"city": "Atlanta", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Give me weather for San Francisco", {"city": "San Francisco", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather report for Houston", {"city": "Houston", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's it in Tampa?", {"city": "Tampa", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's happening weather-wise in Detroit?", {"city": "Detroit", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Check weather in Baltimore", {"city": "Baltimore", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather conditions in Cleveland", {"city": "Cleveland", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Get me the weather in Pittsburgh", {"city": "Pittsburgh", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather looking like in Charlotte?", {"city": "Charlotte", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather out in Miami?", {"city": "Miami", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Forecast for San Jose", {"city": "San Jose", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's weather in Indianapolis?", {"city": "Indianapolis", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            # No city, no date - must default to today
            ("What's the weather?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather report", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Give me the weather", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Check the weather", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the forecast?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            # Explicit today - with date word
            ("Weather right now", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Current weather", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather today?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it raining today?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Seattle today?", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Austin right now", {"city": "Austin", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Temperature in Boston today", {"city": "Boston", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it windy in Chicago today?", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather like in metric units?", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Give me the weather in imperial units", {"unit_system": "imperial", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Forecast for Los Angeles tomorrow", {"city": "Los Angeles", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Weather in Denver tomorrow", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Will it be sunny in Phoenix tomorrow?", {"city": "Phoenix", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Forecast for San Diego the day after tomorrow", {"city": "San Diego", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Weather for Portland the day after tomorrow", {"city": "Portland", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("What's the forecast for Dallas this weekend?", {"city": "Dallas", "resolved_datetimes": weekend_dates}, False),
            ("Weekend forecast for Seattle", {"city": "Seattle", "resolved_datetimes": weekend_dates}, False),
            ("How will the weather be this weekend?", {"resolved_datetimes": weekend_dates}, False),
            ("Forecast for Saturday", {"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("What's the weather on Sunday?", {"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("Weather in New York today", {"city": "New York", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Current conditions in San Francisco", {"city": "San Francisco", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it hot in Las Vegas today?", {"city": "Las Vegas", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather for Orlando tomorrow", {"city": "Orlando", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Forecast for Nashville tomorrow", {"city": "Nashville", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("What's the weather like in Phoenix in metric?", {"city": "Phoenix", "unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather for Boston in imperial units today", {"city": "Boston", "unit_system": "imperial", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it cold in Minneapolis today?", {"city": "Minneapolis", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Forecast for Atlanta tomorrow morning", {"city": "Atlanta", "resolved_datetimes": [RelativeDateKeys.TOMORROW_MORNING]}, False),
            ("Weather in Houston tomorrow", {"city": "Houston", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("What's the forecast for Chicago this weekend?", {"city": "Chicago", "resolved_datetimes": weekend_dates}, False),
            ("Weather for Salt Lake City today", {"city": "Salt Lake City", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it raining in Portland today?", {"city": "Portland", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            # Casual/varied phrasings (no explicit weather word)
            ("Gonna rain?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Need an umbrella?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's it looking outside?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Should I bring a jacket?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Warm enough for shorts?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it nice out?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Beach weather today?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Sweater weather?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's it doing outside?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Can I grill tonight?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
        ]
        return [
            CommandExample(voice_command=voice, expected_parameters=params, is_primary=is_primary)
            for voice, params, is_primary in examples
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("city", "string", required=False, default=None, description="City name as spoken. Optional; omit to use the user's default location."),
            JarvisParameter("resolved_datetimes", "array<datetime>", required=True, description="ISO UTC start-of-day datetimes for requested days (max 5). Required; include today for current weather."),
            JarvisParameter("unit_system", "string", required=False, default=None, description="Unit system: 'metric' or 'imperial'. Omit to use the user's default."),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret("OPENWEATHER_API_KEY", "Open Weather API Key", "integration", "string"),
            JarvisSecret("OPENWEATHER_UNITS", "Imperial, Metric, or Kelvin", "integration", "string"),
            JarvisSecret("OPENWEATHER_LOCATION", "city,state_code,country_code, ie Miami,FL,US. If omitted and no location is found in the command, location will be retrieved from ip-api.com", "node", "string")
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use this command ONLY for weather-related queries (temperature, conditions, forecast, precipitation)",
            "Always call this tool for weather; do NOT answer from memory or ask follow-up questions",
            "City is optional; resolved_datetimes is required (use today's date for current weather).",
            "Do NOT use this for time queries; those are not weather requests",
            "This command is for meteorological information, not time zones or current time"
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="search_web",
                description="Time queries or non-weather facts."
            )
        ]

    def run(self, request_info, **kwargs) -> CommandResponse:
        api_key = get_secret_value("OPENWEATHER_API_KEY", "integration")
        if not api_key:
            raise Exception("Missing OpenWeather API key. Please set it in your node configuration first.")

        city = kwargs.get("city")
        if not city:
            city = get_secret_value("OPENWEATHER_LOCATION", "node")
        if not city:
            city = get_current_location()
        unit_system = kwargs.get("unit_system") or get_secret_value("OPENWEATHER_UNITS", "integration")
        unit_system = unit_system.lower()
        resolved_datetimes = kwargs.get("resolved_datetimes")

        # Always get coordinates first (needed for OneCall API)
        geocode_url = f"http://api.openweathermap.org/geo/1.0/direct?q={city}&limit=1&appid={api_key}"
        geocode_resp = requests.get(geocode_url)
        geocode_resp.raise_for_status()
        geocode_data = geocode_resp.json()
        if isinstance(geocode_data, list) and geocode_data and "lat" in geocode_data[0] and "lon" in geocode_data[0]:
            lat = geocode_data[0]["lat"]
            lon = geocode_data[0]["lon"]
        else:
            raise Exception("Could not determine location coordinates.")

        # Get OneCall API data (includes current, hourly, and daily)
        onecall_url = f"https://api.openweathermap.org/data/3.0/onecall?lat={lat}&lon={lon}&units={unit_system}&appid={api_key}"
        print(f"Getting weather data for {city} (lat: {lat}, lon: {lon})")
        
        onecall_response = requests.get(onecall_url)
        onecall_response.raise_for_status()
        onecall_data = onecall_response.json()
        
        # Check if API returned an error
        if "error" in onecall_data:
            error_msg = onecall_data.get("message", "Unknown API error")
            raise Exception(f"OpenWeather API error: {error_msg}")
        

        if not resolved_datetimes:
            raise Exception("Missing required resolved_datetimes. Use today's date for current weather.")

        if isinstance(resolved_datetimes, list) and len(resolved_datetimes) == 1:
            try:
                today = datetime.datetime.now().date()
                target_date = datetime.datetime.strptime(
                    extract_date_from_datetime(resolved_datetimes[0]), "%Y-%m-%d"
                ).date()
                if target_date == today:
                    # Extract current weather from OneCall API response
                    if "current" in onecall_data:
                        current = onecall_data["current"]
                        temp = current["temp"]
                        description = current["weather"][0]["description"]
                        humidity = current.get("humidity", "N/A")
                        wind_speed = current.get("wind_speed", "N/A")
                        
                        # Return raw data - server will format the message
                        return CommandResponse.success_response(
                            context_data={
                                "city": city,
                                "temperature": temp,
                                "description": description,
                                "humidity": humidity,
                                "wind_speed": wind_speed,
                                "unit_system": unit_system,
                                "weather_type": "current"
                            }
                        )
            except Exception:
                pass

        # Handle forecast for specific datetimes
        # Parse the datetimes parameter - extract dates from datetimes if needed
        target_dates = []
        try:
            if isinstance(resolved_datetimes, list):
                # Extract dates from datetime strings
                date_strings = extract_dates_from_datetimes(resolved_datetimes)
                for date_str in date_strings:
                    if date_str:
                        target_dates.append(datetime.datetime.strptime(date_str, "%Y-%m-%d").date())
                    else:
                        raise Exception("Invalid datetime format in array. Expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS format.")
            else:
                # Handle single date/datetime (convert to list for consistency)
                if isinstance(resolved_datetimes, str):
                    date_str = extract_date_from_datetime(resolved_datetimes)
                    if date_str:
                        target_dates.append(datetime.datetime.strptime(date_str, "%Y-%m-%d").date())
                    else:
                        raise Exception("Invalid datetime format. Expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS format.")
                else:
                    raise Exception("Invalid datetimes parameter type. Expected string or list of strings.")
        except ValueError:
            raise Exception("Invalid datetime format. Expected YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS format.")

        print(f"Looking for weather on: {target_dates}")

        if "daily" in onecall_data:
            # Find the forecasts for the specific dates
            target_forecasts = []
            print("Available dates in forecast:")
            for day in onecall_data["daily"]:
                day_date = datetime.datetime.fromtimestamp(day["dt"]).date()
                print(f"  {day_date} (timestamp: {day['dt']})")
                if day_date in target_dates:
                    target_forecasts.append(day)
                    print(f"Found match for {day_date}!")

            if target_forecasts:
                # Build forecast summary for all requested dates
                forecast_summaries = []
                all_forecast_data = []

                for target_forecast in target_forecasts:
                    # Format the date nicely
                    forecast_date = datetime.datetime.fromtimestamp(target_forecast["dt"]).date()
                    formatted_date = forecast_date.strftime("%A, %B %d")

                    # Extract weather details
                    high_temp = target_forecast["temp"]["max"]
                    low_temp = target_forecast["temp"]["min"]
                    description = target_forecast["weather"][0]["description"]
                    pop = target_forecast.get("pop", 0)  # Probability of precipitation

                    # Create forecast summary for this date
                    date_summary = f"{formatted_date}: High {round(high_temp)}°, Low {round(low_temp)}° with {description}"
                    if pop > 0:
                        date_summary += f" ({int(pop * 100)}% chance of rain)"

                    forecast_summaries.append(date_summary)
                    all_forecast_data.append({
                        "date": formatted_date,
                        "high_temp": high_temp,
                        "low_temp": low_temp,
                        "description": description,
                        "pop": pop
                    })

                # Join all summaries
                full_forecast_summary = '; '.join(forecast_summaries)

                # Return raw data - server will format the message
                return CommandResponse.success_response(
                    context_data={
                        "city": city,
                        "dates": [data["date"] for data in all_forecast_data],
                        "forecast_summary": full_forecast_summary,
                        "forecast_details": all_forecast_data,
                        "unit_system": unit_system,
                        "weather_type": "forecast"
                    }
                )

            # Dates not found in forecast (beyond 8 days)
            if len(target_dates) == 1:
                message = f"I couldn't find weather data for {target_dates[0].strftime('%B %d, %Y')}. The forecast only covers the next 8 days."
            else:
                date_strings = [d.strftime('%B %d') for d in target_dates]
                message = f"I couldn't find weather data for {', '.join(date_strings)}. The forecast only covers the next 8 days."

            print(message)
            speak(message)
            return CommandResponse.error_response(
                error_details="Dates not found in forecast",
                context_data={
                    "city": city,
                    "dates": [d.strftime("%Y-%m-%d") for d in target_dates],
                    "forecast": None,
                    "unit_system": unit_system,
                    "weather_type": "forecast"
                }
            )

        message = "I couldn't retrieve the weather forecast."
        print(message)
        speak(message)
        return CommandResponse.error_response(
            error_details="Could not retrieve weather forecast",
            context_data={
                "forecast": None,
                "unit_system": unit_system,
                "weather_type": "forecast"
            }
        )


def get_current_location():
    try:
        response = requests.get("http://ip-api.com/json/", timeout=5)
        if response.status_code == 200:
            data = response.json()
            city = data.get("city")
            region = data.get("region")
            country = data.get("countryCode")
            if city and region and country:
                return f"{city},{region},{country}"
            elif city and country:
                return f"{city},{country}"
            return city
        return None
    except requests.exceptions.RequestException as e:
        print(f"Error getting location: {e}")
        return None
