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
        return ["weather", "forecast", "temperature", "rain", "snow", "wind", "conditions", "hot", "cold", "sunny", "cloudy", "metric units", "imperial units"]

    @property
    def description(self) -> str:
        return "Retrieve current weather conditions or up-to-5-day forecast. Use for ALL weather-related queries, including those requesting metric or imperial units."

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
            CommandExample(
                voice_command="What's the weather like in metric units?",
                expected_parameters={"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Optimized for 3B model pattern matching:
        - Heavy repetition of "city extraction" patterns
        - Always include resolved_datetimes (required param)
        - Clear "no date = today" pattern reinforcement
        """
        examples = [
            # === CRITICAL: City extraction from "in [CITY]" pattern ===
            ("Weather in Denver", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.TODAY]}, True),
            ("Weather in Seattle", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Phoenix", {"city": "Phoenix", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Boston", {"city": "Boston", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Dallas", {"city": "Dallas", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in Austin?", {"city": "Austin", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in Portland?", {"city": "Portland", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in San Francisco?", {"city": "San Francisco", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Atlanta?", {"city": "Atlanta", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Houston?", {"city": "Houston", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Philadelphia?", {"city": "Philadelphia", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === CRITICAL: No city, no date → defaults to today ===
            ("What's the weather?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Check the weather", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather report", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's it like outside?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's it outside?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === City + explicit "today" ===
            ("What's the weather in Tampa today?", {"city": "Tampa", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Orlando today?", {"city": "Orlando", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Nashville today", {"city": "Nashville", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Current weather in Detroit", {"city": "Detroit", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the current weather in Minneapolis?", {"city": "Minneapolis", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === City + "tomorrow" ===
            ("Weather in Denver tomorrow", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("What's the weather in Seattle tomorrow?", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("What's the forecast for Austin tomorrow?", {"city": "Austin", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("What's the forecast for Los Angeles tomorrow?", {"city": "Los Angeles", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("What's the forecast for Chicago tomorrow?", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("What's the forecast for Miami tomorrow?", {"city": "Miami", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Tomorrow's weather in San Diego", {"city": "San Diego", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Will it rain in Cleveland tomorrow?", {"city": "Cleveland", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),

            # === "Forecast" keyword patterns ===
            ("What's the forecast for Portland?", {"city": "Portland", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Forecast for Dallas", {"city": "Dallas", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the forecast?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Give me the forecast for tomorrow", {"resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),

            # === Weekend patterns (CRITICAL: "this weekend" = this_weekend, NOT next_weekend) ===
            ("Weekend weather in Houston", {"city": "Houston", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("What's the weather this weekend?", {"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("Forecast for this weekend in Tampa", {"city": "Tampa", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("What is the forecast for Seattle this weekend", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("How's the weather this weekend in Denver?", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("This weekend's weather in Chicago", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("Weather for this weekend", {"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("Will it rain this weekend?", {"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),

            # === Day after tomorrow (CRITICAL: must resolve to day_after_tomorrow, NOT tomorrow) ===
            ("Weather the day after tomorrow", {"resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Weather in Boston the day after tomorrow", {"city": "Boston", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Weather forecast for Chicago on the day after tomorrow", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("What's the forecast the day after tomorrow?", {"resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("What's the weather the day after tomorrow in Phoenix?", {"city": "Phoenix", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Forecast for the day after tomorrow in Dallas", {"city": "Dallas", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Day after tomorrow weather in Miami", {"city": "Miami", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("What's the weather the day after tomorrow in Denver?", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Forecast for day after tomorrow in Seattle", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Day after tomorrow forecast for Atlanta", {"city": "Atlanta", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),

            # === Specific day of week ===
            ("Weather on Monday", {"resolved_datetimes": [RelativeDateKeys.NEXT_MONDAY]}, False),
            ("Weather in Phoenix on Friday", {"city": "Phoenix", "resolved_datetimes": [RelativeDateKeys.NEXT_FRIDAY]}, False),
            ("What's the weather Saturday?", {"resolved_datetimes": [RelativeDateKeys.NEXT_SATURDAY]}, False),

            # === Unit system (CRITICAL: "metric units" / "imperial units" = weather, NOT convert_measurement) ===
            ("Weather in metric", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Celsius", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Denver in metric", {"city": "Denver", "unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather like in metric units?", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in metric units?", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Show the weather in metric units", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in metric units", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Give me the weather in imperial units", {"unit_system": "imperial", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in imperial units", {"unit_system": "imperial", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in imperial?", {"unit_system": "imperial", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Show weather in Fahrenheit", {"unit_system": "imperial", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather forecast in metric units", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the forecast in metric units?", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Denver in metric units", {"city": "Denver", "unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in Boston in metric units?", {"city": "Boston", "unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === Condition-specific questions (still weather) ===
            ("Is it raining?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Will it rain today?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it cold outside?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How hot is it?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How hot is it in Las Vegas?", {"city": "Las Vegas", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === Casual/implied weather (umbrella, jacket) ===
            ("Should I bring an umbrella?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Do I need a jacket?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === REINFORCEMENT: "How's the weather in [CITY] today?" (search_web confusion) ===
            ("How's the weather in New York today?", {"city": "New York", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Chicago today?", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Los Angeles today?", {"city": "Los Angeles", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Miami today?", {"city": "Miami", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Dallas today?", {"city": "Dallas", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in Boston today?", {"city": "Boston", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather in Denver today?", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather like in San Francisco today?", {"city": "San Francisco", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === REINFORCEMENT: "[CITY] forecast this weekend" (search_web confusion) ===
            ("What's the forecast for New York this weekend?", {"city": "New York", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("What's the forecast for Chicago this weekend?", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("What's the forecast for Los Angeles this weekend?", {"city": "Los Angeles", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("What's the forecast for Miami this weekend?", {"city": "Miami", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("Weather forecast for Dallas this weekend", {"city": "Dallas", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("What's the weather forecast for Portland this weekend?", {"city": "Portland", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),

            # === REINFORCEMENT: "What's the weather like?" no city (param extraction) ===
            ("What's the weather like?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather like today?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the weather like outside?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
        ]
        return [
            CommandExample(voice_command=voice, expected_parameters=params, is_primary=is_primary)
            for voice, params, is_primary in examples
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("city", "string", required=False, default=None, description="City name as spoken. Optional; omit to use the user's default location."),
            JarvisParameter("resolved_datetimes", "array<datetime>", required=True, description="ISO UTC start-of-day datetimes for requested days (max 5). Always required; include today for current weather."),
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
                description="Time queries ('What time is it in Washington?', 'Current time in Dubai'), time zones, sunrise/sunset times, or non-weather facts."
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
