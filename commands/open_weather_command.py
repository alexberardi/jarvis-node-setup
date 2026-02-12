import datetime
import requests
from typing import List, Any, Optional

from jarvis_log_client import JarvisLogger

from constants.relative_date_keys import RelativeDateKeys
from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from scripts.text_to_speech import speak
from services.secret_service import get_secret_value
from utils.date_util import extract_dates_from_datetimes, extract_date_from_datetime

logger = JarvisLogger(service="jarvis-node")


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
        """Generate concise example utterances with expected parameters using date keys.

        IMPORTANT: The primary example uses "What's the weather in [CITY]?" pattern
        to reinforce city extraction (the model has confused this with query parameter).
        """
        return [
            CommandExample(
                voice_command="What's the weather in Chicago?",
                expected_parameters={"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.TODAY]},
                is_primary=True
            ),
            CommandExample(
                voice_command="What's the weather like?",
                expected_parameters={"resolved_datetimes": [RelativeDateKeys.TODAY]}
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

        Focus areas:
        - Implicit today (no date word -> resolved_datetimes: ["today"])
        - City extraction with various phrasings
        - Day after tomorrow as single token
        """
        examples = [
            # === IMPLICIT TODAY - Critical pattern: no date word = today ===
            ("What's the weather like?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, True),
            ("What's the weather?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather report", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the forecast?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === IMPLICIT TODAY + CITY - no date word, but city present ===
            ("What's the weather in Miami?", {"city": "Miami", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in Seattle?", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Denver", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Weather in Chicago", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("What's the forecast for Boston?", {"city": "Boston", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Temperature in Phoenix", {"city": "Phoenix", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it raining in Portland?", {"city": "Portland", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How hot is it in Dallas?", {"city": "Dallas", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === EXPLICIT TODAY ===
            ("What's the weather today?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("How's the weather in New York today?", {"city": "New York", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Current weather in Austin", {"city": "Austin", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === TOMORROW ===
            ("What's the forecast for Los Angeles tomorrow?", {"city": "Los Angeles", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Weather in Denver tomorrow", {"city": "Denver", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),
            ("Will it rain tomorrow?", {"resolved_datetimes": [RelativeDateKeys.TOMORROW]}, False),

            # === DAY AFTER TOMORROW - single token, NOT two separate dates ===
            ("Weather forecast for Chicago on the day after tomorrow", {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("What's the weather the day after tomorrow?", {"resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),
            ("Forecast for San Diego the day after tomorrow", {"city": "San Diego", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}, False),

            # === WEEKEND ===
            ("What's the weather this weekend?", {"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),
            ("Weekend forecast for Seattle", {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}, False),

            # === UNIT SYSTEM ===
            ("What's the weather in metric units?", {"unit_system": "metric", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Give me the weather in imperial units", {"unit_system": "imperial", "resolved_datetimes": [RelativeDateKeys.TODAY]}, False),

            # === CASUAL PHRASINGS ===
            ("Is it raining?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Should I bring an umbrella?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
            ("Is it cold outside?", {"resolved_datetimes": [RelativeDateKeys.TODAY]}, False),
        ]
        return [
            CommandExample(voice_command=voice, expected_parameters=params, is_primary=is_primary)
            for voice, params, is_primary in examples
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("city", "string", required=False, default=None, description="City name as spoken. Optional; omit to use the user's default location."),
            JarvisParameter("resolved_datetimes", "array<string>", required=True, description="Date keys like 'today', 'tomorrow', 'this_weekend' (max 5 days). Always required; use 'today' for current weather."),
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
            "Extract the city name into the 'city' parameter (NOT 'query'). Example: 'What's the weather in Miami?' -> city='Miami'",
            "resolved_datetimes is required (use today's date for current weather). city is optional.",
            "Do NOT use this for time queries; those are not weather requests",
            "This tool has NO 'query' parameter. Use 'city' for location and 'unit_system' for units."
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="get_current_time",
                description="Time queries ('What time is it in [location]?', 'Current time in Dubai'), time zones. Use get_current_time for all time-related queries."
            ),
            CommandAntipattern(
                command_name="search_web",
                description="Sunrise/sunset times, non-weather facts, current events."
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
        logger.debug("Getting weather data", city=city, lat=lat, lon=lon)
        
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
            except Exception as e:
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

        logger.debug("Looking for weather on dates", dates=str(target_dates))

        if "daily" in onecall_data:
            # Find the forecasts for the specific dates
            target_forecasts = []
            logger.debug("Searching for matching forecast dates")
            for day in onecall_data["daily"]:
                day_date = datetime.datetime.fromtimestamp(day["dt"]).date()
                if day_date in target_dates:
                    target_forecasts.append(day)
                    logger.debug("Found forecast match", date=str(day_date))

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

            logger.warning("Dates not found in forecast", message=message)
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
        logger.warning("Could not retrieve weather forecast")
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
        logger.error("Error getting location", error=str(e))
        return None
