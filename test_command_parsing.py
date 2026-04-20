#!/usr/bin/env python3
"""
Test script for testing command parsing across all Jarvis commands.
This script tests various natural language utterances to ensure proper parameter extraction.
"""

from __future__ import annotations

import ast
import datetime
import json
import time
from typing import Dict, Any, List, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from utils.command_execution_service import ParseResult
from zoneinfo import ZoneInfo
import argparse
from dotenv import load_dotenv

from clients.responses.jarvis_command_center import DateContext
from constants.relative_date_keys import RelativeDateKeys, ALL_DATE_KEYS

# Load environment variables (this will load CONFIG_PATH from .env)
load_dotenv()

class CommandTest:
    def __init__(self, voice_command: str, expected_command: str, expected_params: Dict[str, Any], description: str, ha_context: Optional[Dict[str, Any]] = None):
        self.voice_command = voice_command
        self.expected_command = expected_command
        self.expected_params = expected_params
        self.description = description
        self.ha_context = ha_context  # Optional HA agent data to inject


# Mock Home Assistant context for device control tests.
# get_ha_entities server tool reads this from the conversation cache.
MOCK_HA_CONTEXT = {
    "home_assistant": {
        "light_controls": {
            "Living Room": {
                "entity_id": "light.living_room",
                "state": "on",
                "type": "room_group",
                "area": "Living Room",
            },
            "Kitchen": {
                "entity_id": "light.kitchen",
                "state": "off",
                "type": "room_group",
                "area": "Kitchen",
            },
            "Bedroom": {
                "entity_id": "light.bedroom",
                "state": "off",
                "type": "room_group",
                "area": "Bedroom",
            },
        },
        "device_controls": {
            "light": [
                {"entity_id": "light.office_desk", "name": "Office Desk Light", "state": "on", "area": "Office"},
            ],
            "switch": [
                {"entity_id": "switch.coffee_maker", "name": "Coffee Maker", "state": "off", "area": "Kitchen"},
            ],
            "lock": [
                {"entity_id": "lock.front_door", "name": "Front Door Lock", "state": "locked", "area": "Entryway"},
                {"entity_id": "lock.back_door", "name": "Back Door Lock", "state": "locked", "area": "Backyard"},
            ],
            "cover": [
                {"entity_id": "cover.garage_door", "name": "Garage Door", "state": "closed", "area": "Garage"},
            ],
            "climate": [
                {
                    "entity_id": "climate.thermostat",
                    "name": "Thermostat",
                    "state": "heat",
                    "area": "Living Room",
                    "current_temperature": 68,
                    "target_temperature": 72,
                },
            ],
            "fan": [
                {"entity_id": "fan.bedroom_fan", "name": "Bedroom Fan", "state": "off", "area": "Bedroom"},
            ],
            "scene": [
                {"entity_id": "scene.movie_time", "name": "Movie Time", "area": "Living Room"},
                {"entity_id": "scene.goodnight", "name": "Goodnight", "area": "Bedroom"},
            ],
        },
        "floors": {
            "Downstairs": ["Living Room", "Kitchen", "Garage", "Entryway"],
            "Upstairs": ["Bedroom", "Office", "Backyard"],
        },
        "device_count": 12,
        "areas": ["Living Room", "Kitchen", "Bedroom", "Office", "Garage", "Entryway", "Backyard"],
    },
}


def _entities_from_ha_context(ha_context: Dict[str, Any]) -> Dict[str, str]:
    """Extract entity_id -> friendly_name map from test HA context.

    Mirrors ControlDeviceCommand._get_known_entities() but reads from
    the test's MOCK_HA_CONTEXT instead of agent_scheduler.
    """
    if not ha_context:
        return {}

    ha_data = ha_context.get("home_assistant", {})
    entities: Dict[str, str] = {}

    for name, info in ha_data.get("light_controls", {}).items():
        eid = info.get("entity_id", "")
        if eid:
            entities[eid] = name

    for domain_devices in ha_data.get("device_controls", {}).values():
        for dev in domain_devices:
            if dev.get("state") == "unavailable":
                continue
            eid = dev.get("entity_id", "")
            if eid and eid not in entities:
                entities[eid] = dev.get("name", "")

    return entities


def create_test_commands() -> List[CommandTest]:
    """Create a comprehensive list of test commands covering various scenarios"""

    # Delegate to the context-aware version with no date context
    return create_test_commands_with_context(None)

def create_test_commands_with_context(date_context: Optional[DateContext]) -> List[CommandTest]:
    """Create test commands using the provided DateContext"""
    
    tests = []
    
    # # ===== OPEN WEATHER COMMAND TESTS =====
    weather_tests = [
        CommandTest(
            "What's the weather like?",
            "get_weather",
            {},
            "Basic current weather request (no city; backend defaults to today)"
        ),
        CommandTest(
            "What's the weather in Miami?",
            "get_weather",
            {"city": "Miami"},
            "Current weather with city specified (backend defaults to today)"
        ),
        CommandTest(
            "How's the weather in New York today?",
            "get_weather",
            {"city": "New York", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Current weather with city and relative date (treated as current)"
        ),
        CommandTest(
            "What's the forecast for Los Angeles tomorrow?",
            "get_weather",
            {"city": "Los Angeles", "resolved_datetimes": [RelativeDateKeys.TOMORROW]},
            "Forecast with city and relative date"
        ),
        CommandTest(
            "Weather forecast for Chicago on the day after tomorrow",
            "get_weather",
            {"city": "Chicago", "resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]},
            "Forecast with city and specific relative date"
        ),
        CommandTest(
            "What's the weather like in metric units?",
            "get_weather",
            {"unit_system": "metric"},
            "Current weather with unit system specified"
        ),
        CommandTest(
            "What is the forecast for Seattle this weekend",
            "get_weather",
            {"city": "Seattle", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]},
            "Forecast with city and date range"
        )
    ]
    tests.extend(weather_tests)
    
    # ===== READ CALENDAR COMMAND TESTS =====
    calendar_tests = [
        CommandTest(
            "What's on my calendar today?",
            "get_calendar_events",
            {"resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Calendar events for today (relative date)"
        ),
        CommandTest(
            "Show me my schedule for tomorrow",
            "get_calendar_events",
            {"resolved_datetimes": [RelativeDateKeys.TOMORROW]},
            "Calendar events for tomorrow (relative date)"
        ),
        CommandTest(
            "What appointments do I have the day after tomorrow?",
            "get_calendar_events",
            {"resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]},
            "Calendar events for day after tomorrow (relative date)"
        ),
        CommandTest(
            "Show my calendar for this weekend",
            "get_calendar_events",
            {"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]},
            "Calendar events for date range"
        ),
        CommandTest(
            "What meetings do I have next week?",
            "get_calendar_events",
            {"resolved_datetimes": [RelativeDateKeys.NEXT_WEEK]},
            "Calendar events for week range"
        ),
        CommandTest(
            "Read my calendar",
            "get_calendar_events",
            {"resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Basic calendar request (uses today)"
        )
    ]
    tests.extend(calendar_tests)
    
    # ===== GENERAL KNOWLEDGE COMMAND TESTS =====
    knowledge_tests = [
        CommandTest(
            "What is the capital of France?",
            "answer_question",
            {"query": "What is the capital of France?"},
            "Basic knowledge question"
        ),
        CommandTest(
            "Who was Albert Einstein?",
            "answer_question",
            {"query": "Who was Albert Einstein?"},
            "Person-related knowledge question"
        ),
        CommandTest(
            "When did World War II end?",
            "answer_question",
            {"query": "When did World War II end?"},
            "Historical knowledge question"
        ),
        CommandTest(
            "How does photosynthesis work?",
            "answer_question",
            {"query": "How does photosynthesis work?"},
            "Science knowledge question"
        ),
        CommandTest(
            "Where is Mount Everest located?",
            "answer_question",
            {"query": "Where is Mount Everest located?"},
            "Geography knowledge question"
        ),
        CommandTest(
            "Explain quantum physics",
            "answer_question",
            {"query": "Explain quantum physics"},
            "Complex topic explanation request"
        )
    ]
    tests.extend(knowledge_tests)
    
    # ===== WEB SEARCH COMMAND TESTS =====
    web_search_tests = [
        CommandTest(
            "Who won the senate election in Pennsylvania?",
            "search_web",
            {"query": "Who won the senate election in Pennsylvania?"},
            "Current election results search"
        ),
        CommandTest(
            "What time is it in California?",
            "get_current_time",
            {"location": "California"},
            "Timezone/current time query"
        ),
        CommandTest(
            "What's the latest news about Tesla stock?",
            "search_web",
            {"query": "What's the latest news about Tesla stock?"},
            "Current market/news search"
        ),
        CommandTest(
            "When is the next SpaceX launch?",
            "search_web",
            {"query": "When is the next SpaceX launch?"},
            "Upcoming event search"
        ),
        CommandTest(
            "Who won the Super Bowl this year?",
            "search_web",
            {"query": "Who won the Super Bowl this year?"},
            "Sports championship question (no team name → validation redirects to search_web)"
        ),
        CommandTest(
            "What's the current weather in Miami?",
            "get_weather",
            {"city": "Miami"},
            "Current weather query (should use weather command, not web search)"
        ),
        CommandTest(
            "Search for breaking news about artificial intelligence",
            "search_web",
            {"query": "Search for breaking news about artificial intelligence"},
            "Explicit search request with current topic"
        ),
        CommandTest(
            "Find the latest information about COVID vaccines",
            "search_web",
            {"query": "Find the latest information about COVID vaccines"},
            "Current health information search"
        ),
        # Pop culture
        CommandTest(
            "Who won the Oscar for best picture this year?",
            "search_web",
            {"query": "Who won the Oscar for best picture this year?"},
            "Award show results (pop culture)"
        ),
        CommandTest(
            "Who won the Grammy for album of the year?",
            "search_web",
            {"query": "Who won the Grammy for album of the year?"},
            "Music award results"
        ),
        # World news
        CommandTest(
            "What's happening with the war in Ukraine?",
            "search_web",
            {"query": "What's happening with the war in Ukraine?"},
            "World news current events"
        ),
        # Elections
        CommandTest(
            "Who is leading in the polls for president?",
            "search_web",
            {"query": "Who is leading in the polls for president?"},
            "Election polling search"
        ),
    ]
    tests.extend(web_search_tests)
    
    # ===== TELL A JOKE COMMAND TESTS =====
    joke_tests = [
        CommandTest(
            "Tell me a joke",
            "tell_joke",
            {},
            "Basic joke request (no topic)"
        ),
        CommandTest(
            "Tell me a joke about programming",
            "tell_joke",
            {"topic": "programming"},
            "Joke with specific topic"
        ),
        CommandTest(
            "Tell me a joke about animals",
            "tell_joke",
            {"topic": "animals"},
            "Joke with different topic"
        ),
        CommandTest(
            "Make me laugh with a joke about technology",
            "tell_joke",
            {"topic": "technology"},
            "Joke with topic using different phrasing"
        )
    ]
    tests.extend(joke_tests)
    
    # ===== CALCULATOR COMMAND TESTS =====
    calculator_tests = [
        CommandTest(
            "What's 5 plus 3?",
            "calculate",
            {"num1": 5, "num2": 3, "operation": "add"},
            "Basic addition calculation"
        ),
        CommandTest(
            "Calculate 10 minus 4",
            "calculate",
            {"num1": 10, "num2": 4, "operation": "subtract"},
            "Subtraction calculation with different phrasing"
        ),
        CommandTest(
            "What is 6 times 7?",
            "calculate",
            {"num1": 6, "num2": 7, "operation": "multiply"},
            "Multiplication calculation with question format"
        ),
        CommandTest(
            "Divide 20 by 5",
            "calculate",
            {"num1": 20, "num2": 5, "operation": "divide"},
            "Division calculation with direct command"
        ),
        CommandTest(
            "Add 15 and 25 together",
            "calculate",
            {"num1": 15, "num2": 25, "operation": "add"},
            "Addition with 'and' conjunction"
        ),
        CommandTest(
            "What's the sum of 8 and 12?",
            "calculate",
            {"num1": 8, "num2": 12, "operation": "add"},
            "Addition using 'sum' terminology"
        ),
        CommandTest(
            "What's five plus 3?",
            "calculate",
            {"num1": 5, "num2": 3, "operation": "add"},
            "Addition with one written number (five) and one numeric (3)"
        ),
        CommandTest(
            "Calculate ten minus four",
            "calculate",
            {"num1": 10, "num2": 4, "operation": "subtract"},
            "Subtraction with both numbers written as words"
        )
    ]
    tests.extend(calculator_tests)
    
    # ===== MEASUREMENT CONVERSION COMMAND TESTS =====
    conversion_tests = [
        # Distance conversions - going up the tree (smaller to larger)
        CommandTest(
            "How many inches in a mile?",
            "convert_measurement",
            {"value": 1, "from_unit": "miles", "to_unit": "inches"},
            "Distance conversion up tree: miles to inches (1 mile = many inches)"
        ),
        CommandTest(
            "How many feet in a mile?",
            "convert_measurement",
            {"value": 1, "from_unit": "miles", "to_unit": "feet"},
            "Distance conversion up tree: miles to feet"
        ),
        CommandTest(
            "How many centimeters in a meter?",
            "convert_measurement",
            {"value": 1, "from_unit": "meters", "to_unit": "centimeters"},
            "Distance conversion up tree: meters to centimeters"
        ),
        
        # Distance conversions - going down the tree (larger to smaller)
        CommandTest(
            "How many miles in 1000 feet?",
            "convert_measurement",
            {"value": 1000, "from_unit": "feet", "to_unit": "miles"},
            "Distance conversion down tree: feet to miles"
        ),
        CommandTest(
            "How many yards in 3 feet?",
            "convert_measurement",
            {"value": 3, "from_unit": "feet", "to_unit": "yards"},
            "Distance conversion down tree: feet to yards"
        ),
        
        # Volume conversions - going up the tree (smaller to larger)
        CommandTest(
            "How many cups in a gallon?",
            "convert_measurement",
            {"value": 1, "from_unit": "gallons", "to_unit": "cups"},
            "Volume conversion up tree: gallons to cups"
        ),
        CommandTest(
            "How many tablespoons in a cup?",
            "convert_measurement",
            {"value": 1, "from_unit": "cups", "to_unit": "tablespoons"},
            "Volume conversion up tree: cups to tablespoons"
        ),
        
        # Volume conversions - going down the tree (larger to smaller)
        CommandTest(
            "How many gallons in 8 pints?",
            "convert_measurement",
            {"value": 8, "from_unit": "pints", "to_unit": "gallons"},
            "Volume conversion down tree: pints to gallons"
        ),
        CommandTest(
            "How many quarts in 2 gallons?",
            "convert_measurement",
            {"value": 2, "from_unit": "gallons", "to_unit": "quarts"},
            "Volume conversion down tree: gallons to quarts"
        ),
        
        # Cross-system conversions
        CommandTest(
            "Convert 5 miles to kilometers",
            "convert_measurement",
            {"value": 5, "from_unit": "miles", "to_unit": "kilometers"},
            "Cross-system conversion: imperial miles to metric kilometers"
        ),
        CommandTest(
            "Convert 100 meters to yards",
            "convert_measurement",
            {"value": 100, "from_unit": "meters", "to_unit": "yards"},
            "Cross-system conversion: metric meters to imperial yards"
        ),
        CommandTest(
            "Convert 10 pounds to kilograms",
            "convert_measurement",
            {"value": 10, "from_unit": "pounds", "to_unit": "kilograms"},
            "Cross-system conversion: imperial pounds to metric kilograms"
        ),
        CommandTest(
            "Convert 2 liters to gallons",
            "convert_measurement",
            {"value": 2, "from_unit": "liters", "to_unit": "gallons"},
            "Cross-system conversion: metric liters to imperial gallons"
        ),
        
        # Temperature conversions
        CommandTest(
            "What's 350 Fahrenheit in Celsius?",
            "convert_measurement",
            {"value": 350, "from_unit": "fahrenheit", "to_unit": "celsius"},
            "Temperature conversion: Fahrenheit to Celsius"
        ),
        CommandTest(
            "Convert 25 Celsius to Fahrenheit",
            "convert_measurement",
            {"value": 25, "from_unit": "celsius", "to_unit": "fahrenheit"},
            "Temperature conversion: Celsius to Fahrenheit"
        ),
        
        # Weight conversions
        CommandTest(
            "How many grams in 3 ounces?",
            "convert_measurement",
            {"value": 3, "from_unit": "ounces", "to_unit": "grams"},
            "Weight conversion: ounces to grams"
        ),
        CommandTest(
            "How many pounds in 2 kilograms?",
            "convert_measurement",
            {"value": 2, "from_unit": "kilograms", "to_unit": "pounds"},
            "Weight conversion: kilograms to pounds"
        ),
        
        # Edge cases and complex conversions
        CommandTest(
            "How many teaspoons in a gallon?",
            "convert_measurement",
            {"value": 1, "from_unit": "gallons", "to_unit": "teaspoons"},
            "Complex volume conversion: gallons to teaspoons (many steps)"
        ),
        CommandTest(
            "Convert 1 league to inches",
            "convert_measurement",
            {"value": 1, "from_unit": "leagues", "to_unit": "inches"},
            "Complex distance conversion: leagues to inches (many steps)"
        )
    ]
    # tests.extend(conversion_tests)
    
    # ===== SPORTS SCORE COMMAND TESTS =====
    sports_tests = [
        CommandTest(
            "How did the Giants do?",
            "get_sports",
            {"team_name": "Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Basic sports score request (no city, no dates)"
        ),
        CommandTest(
            "What's the score of the Yankees game?",
            "get_sports",
            {"team_name": "Yankees", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Sports score request with different team"
        ),
        CommandTest(
            "How did the New York Giants do?",
            "get_sports",
            {"team_name": "New York Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Sports score request with location disambiguation"
        ),
        CommandTest(
            "What's the score of the Carolina Panthers game?",
            "get_sports",
            {"team_name": "Carolina Panthers", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Sports score request with different location/team combination"
        ),
        CommandTest(
            "How did the Giants do yesterday?",
            "get_sports",
            {"team_name": "Giants", "resolved_datetimes": [RelativeDateKeys.YESTERDAY]},
            "Sports score request with relative date"
        ),
        CommandTest(
            "What was the score of the Yankees game yesterday?",
            "get_sports",
            {"team_name": "Yankees", "resolved_datetimes": [RelativeDateKeys.YESTERDAY]},
            "Sports score with relative date"
        ),
        CommandTest(
            "How did the Baltimore Orioles do last weekend?",
            "get_sports",
            {"team_name": "Baltimore Orioles", "resolved_datetimes": [RelativeDateKeys.LAST_WEEKEND]},
            "Sports score with date range"
        ),
        CommandTest(
            "What was the Chicago Bulls score last weekend?",
            "get_sports",
            {"team_name": "Chicago Bulls", "resolved_datetimes": [RelativeDateKeys.LAST_WEEKEND]},
            "Sports score with date range"
        ),
        CommandTest(
            "How did the Cowboys do?",
            "get_sports",
            {"team_name": "Cowboys", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Sports score with explicit today date"
        ),
        CommandTest(
            "What's the score of the Warriors game tomorrow?",
            "get_sports",
            {"team_name": "Warriors", "resolved_datetimes": [RelativeDateKeys.TOMORROW]},
            "Sports score with relative date"
        ),
        CommandTest(
            "What was the score of the Panthers game yesterday?",
            "get_sports",
            {"team_name": "Panthers", "resolved_datetimes": [RelativeDateKeys.YESTERDAY]},
            "Sports score with relative date"
        ),
        CommandTest(
            "How did the Eagles do last weekend?",
            "get_sports",
            {"team_name": "Eagles", "resolved_datetimes": [RelativeDateKeys.LAST_WEEKEND]},
            "Sports score with date range"
        ),
        CommandTest(
            "What's the score of the Lakers game today?",
            "get_sports",
            {"team_name": "Lakers", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Sports score with current date"
        ),
        CommandTest(
            "How did the Buccaneers do?",
            "get_sports",
            {"team_name": "Buccaneers", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Sports score with explicit today date"
        ),
        # Additional flexibility tests - phrasings NOT directly in examples
        CommandTest(
            "Did the Steelers win?",
            "get_sports",
            {"team_name": "Steelers", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Flexibility test: 'Did X win' pattern"
        ),
        CommandTest(
            "What was the Mets score?",
            "get_sports",
            {"team_name": "Mets", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Flexibility test: 'What was the X score' pattern"
        ),
        CommandTest(
            "Final score for the Denver Broncos?",
            "get_sports",
            {"team_name": "Denver Broncos", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            "Flexibility test: 'Final score for X' with full team name"
        ),
        CommandTest(
            "How'd the Packers do last night?",
            "get_sports",
            {"team_name": "Packers", "resolved_datetimes": [RelativeDateKeys.YESTERDAY]},
            "Flexibility test: Contraction 'How'd' with 'last night'"
        )
    ]
    tests.extend(sports_tests)

    # ===== TIMER COMMAND TESTS =====
    timer_tests = [
        # Seconds only
        CommandTest(
            "Set a timer for 30 seconds",
            "set_timer",
            {"duration_seconds": 30},
            "Timer for seconds only"
        ),
        CommandTest(
            "Timer for 45 seconds",
            "set_timer",
            {"duration_seconds": 45},
            "Timer without 'set' prefix"
        ),

        # Minutes only
        CommandTest(
            "Set a timer for 5 minutes",
            "set_timer",
            {"duration_seconds": 300},
            "Timer for 5 minutes (300 seconds)"
        ),
        CommandTest(
            "Timer for ten minutes",
            "set_timer",
            {"duration_seconds": 600},
            "Timer with written-out number"
        ),
        CommandTest(
            "Set a 15 minute timer",
            "set_timer",
            {"duration_seconds": 900},
            "Timer with duration before 'timer'"
        ),

        # Hours only
        CommandTest(
            "Set a timer for 1 hour",
            "set_timer",
            {"duration_seconds": 3600},
            "Timer for 1 hour (3600 seconds)"
        ),
        CommandTest(
            "Timer for 2 hours",
            "set_timer",
            {"duration_seconds": 7200},
            "Timer for 2 hours"
        ),

        # Compound times
        CommandTest(
            "Set a timer for 1 hour and 30 minutes",
            "set_timer",
            {"duration_seconds": 5400},
            "Compound timer: 1h 30m"
        ),
        CommandTest(
            "Timer for 2 minutes 30 seconds",
            "set_timer",
            {"duration_seconds": 150},
            "Compound timer: 2m 30s"
        ),

        # With labels
        CommandTest(
            "Set a 10 minute timer for pasta",
            "set_timer",
            {"duration_seconds": 600, "label": "pasta"},
            "Timer with label"
        ),
        CommandTest(
            "Timer for 20 minutes for the laundry",
            "set_timer",
            {"duration_seconds": 1200, "label": "laundry"},
            "Timer with label (longer phrase)"
        ),
        CommandTest(
            "Set a nap timer for 30 minutes",
            "set_timer",
            {"duration_seconds": 1800, "label": "nap"},
            "Timer with label before duration"
        ),

        # Casual phrasing
        CommandTest(
            "Remind me in 15 minutes",
            "set_timer",
            {"duration_seconds": 900},
            "Casual: 'remind me' phrasing"
        ),
        CommandTest(
            "Wake me up in 30 minutes",
            "set_timer",
            {"duration_seconds": 1800},
            "Casual: 'wake me up' phrasing"
        ),
        CommandTest(
            "Let me know in an hour",
            "set_timer",
            {"duration_seconds": 3600},
            "Casual: 'let me know' phrasing"
        )
    ]
    tests.extend(timer_tests)

    # ===== HOME ASSISTANT CONTROL DEVICE TESTS =====
    # Uses mock HA context injected into conversation start.
    # Model calls get_ha_entities (server tool) to discover entity IDs,
    # then calls control_device/get_device_status (client tools).
    ha_tests = [
        # Light control
        CommandTest(
            "Turn off the living room lights",
            "control_device",
            {"entity_id": "light.living_room", "action": "turn_off"},
            "HA: Turn off room-group light",
            ha_context=MOCK_HA_CONTEXT,
        ),
        CommandTest(
            "Turn on the kitchen lights",
            "control_device",
            {"entity_id": "light.kitchen", "action": "turn_on"},
            "HA: Turn on room-group light",
            ha_context=MOCK_HA_CONTEXT,
        ),
        # Lock control
        CommandTest(
            "Lock the front door",
            "control_device",
            {"entity_id": "lock.front_door", "action": "lock"},
            "HA: Lock a door",
            ha_context=MOCK_HA_CONTEXT,
        ),
        CommandTest(
            "Unlock the back door",
            "control_device",
            {"entity_id": "lock.back_door", "action": "unlock"},
            "HA: Unlock a door",
            ha_context=MOCK_HA_CONTEXT,
        ),
        # Cover control
        CommandTest(
            "Open the garage door",
            "control_device",
            {"entity_id": "cover.garage_door", "action": "open_cover"},
            "HA: Open a cover",
            ha_context=MOCK_HA_CONTEXT,
        ),
        CommandTest(
            "Close the garage",
            "control_device",
            {"entity_id": "cover.garage_door", "action": "close_cover"},
            "HA: Close a cover (short name)",
            ha_context=MOCK_HA_CONTEXT,
        ),
        # Climate control
        CommandTest(
            "Set the thermostat to 72",
            "control_device",
            {"entity_id": "climate.thermostat", "action": "set_temperature"},
            "HA: Set thermostat temperature",
            ha_context=MOCK_HA_CONTEXT,
        ),
        # Switch control
        CommandTest(
            "Turn on the coffee maker",
            "control_device",
            {"entity_id": "switch.coffee_maker", "action": "turn_on"},
            "HA: Turn on a switch",
            ha_context=MOCK_HA_CONTEXT,
        ),
        # Device status queries
        CommandTest(
            "Is the front door locked?",
            "get_device_status",
            {"entity_id": "lock.front_door"},
            "HA: Query lock status",
            ha_context=MOCK_HA_CONTEXT,
        ),
        CommandTest(
            "Is the garage door open?",
            "get_device_status",
            {"entity_id": "cover.garage_door"},
            "HA: Query cover status",
            ha_context=MOCK_HA_CONTEXT,
        ),
    ]
    tests.extend(ha_tests)

    # ===== MUSIC COMMAND TESTS (merged play + control) =====
    # media_type is NOT tested — Music Assistant resolves content type via fuzzy search
    music_tests = [
        # --- Play: Artist ---
        CommandTest(
            "Play Radiohead",
            "music",
            {"action": "play", "query": "Radiohead"},
            "Play artist by name"
        ),
        CommandTest(
            "Put on some Beatles",
            "music",
            {"action": "play", "query": "Beatles"},
            "Play artist with casual phrasing"
        ),
        CommandTest(
            "Play Taylor Swift",
            "music",
            {"action": "play", "query": "Taylor Swift"},
            "Play artist with full name"
        ),

        # --- Play: Album ---
        CommandTest(
            "Play OK Computer",
            "music",
            {"action": "play", "query": "OK Computer"},
            "Play album by name"
        ),
        CommandTest(
            "Play the album Abbey Road",
            "music",
            {"action": "play", "query": "Abbey Road"},
            "Play album with 'the album' prefix"
        ),

        # --- Play: Track ---
        CommandTest(
            "Play Karma Police",
            "music",
            {"action": "play", "query": "Karma Police"},
            "Play track by name"
        ),
        CommandTest(
            "Play the song Bohemian Rhapsody",
            "music",
            {"action": "play", "query": "Bohemian Rhapsody"},
            "Play track with 'the song' prefix"
        ),

        # --- Play: Genre/mood ---
        CommandTest(
            "Play some jazz",
            "music",
            {"action": "play", "query": "jazz"},
            "Play genre"
        ),
        CommandTest(
            "Put on classical music",
            "music",
            {"action": "play", "query": "classical"},
            "Play genre with casual phrasing"
        ),
        CommandTest(
            "Play relaxing music",
            "music",
            {"action": "play", "query": "relaxing"},
            "Play mood-based music"
        ),

        # --- Play: With player ---
        CommandTest(
            "Play Beatles in the kitchen",
            "music",
            {"action": "play", "query": "Beatles", "player": "kitchen"},
            "Play with room/player specified"
        ),
        CommandTest(
            "Play jazz in the living room",
            "music",
            {"action": "play", "query": "jazz", "player": "living room"},
            "Play with room specified"
        ),

        # --- Play: Queue options ---
        CommandTest(
            "Add Bohemian Rhapsody to the queue",
            "music",
            {"action": "play", "query": "Bohemian Rhapsody", "queue_option": "add"},
            "Add to queue"
        ),
        CommandTest(
            "Play Stairway to Heaven next",
            "music",
            {"action": "play", "query": "Stairway to Heaven", "queue_option": "next"},
            "Play track next in queue"
        ),

        # --- Control: Basic playback ---
        CommandTest(
            "Pause the music",
            "music",
            {"action": "pause"},
            "Pause playback"
        ),
        CommandTest(
            "Resume",
            "music",
            {"action": "resume"},
            "Resume playback"
        ),
        CommandTest(
            "Stop the music",
            "music",
            {"action": "stop"},
            "Stop playback"
        ),

        # --- Control: Skip/navigation ---
        CommandTest(
            "Skip this song",
            "music",
            {"action": "next"},
            "Skip to next track"
        ),
        CommandTest(
            "Next song",
            "music",
            {"action": "next"},
            "Next track with different phrasing"
        ),
        CommandTest(
            "Go back",
            "music",
            {"action": "previous"},
            "Previous track"
        ),
        CommandTest(
            "Previous song",
            "music",
            {"action": "previous"},
            "Previous track explicit"
        ),

        # --- Volume ---
        CommandTest(
            "Turn up the volume",
            "music",
            {"action": "volume_up"},
            "Volume up"
        ),
        CommandTest(
            "Louder",
            "music",
            {"action": "volume_up"},
            "Volume up casual"
        ),
        CommandTest(
            "Turn it down",
            "music",
            {"action": "volume_down"},
            "Volume down"
        ),
        CommandTest(
            "Quieter",
            "music",
            {"action": "volume_down"},
            "Volume down casual"
        ),
        CommandTest(
            "Set volume to 50",
            "music",
            {"action": "volume_set", "volume_level": 50},
            "Set specific volume"
        ),
        CommandTest(
            "Mute",
            "music",
            {"action": "mute"},
            "Mute audio"
        ),

        # --- Mode: Shuffle and repeat ---
        CommandTest(
            "Turn on shuffle",
            "music",
            {"action": "shuffle_on"},
            "Enable shuffle"
        ),
        CommandTest(
            "Shuffle off",
            "music",
            {"action": "shuffle_off"},
            "Disable shuffle"
        ),
        CommandTest(
            "Repeat this song",
            "music",
            {"action": "repeat_one"},
            "Repeat current track"
        ),
        CommandTest(
            "Stop repeating",
            "music",
            {"action": "repeat_off"},
            "Disable repeat"
        ),

        # --- Control: With player ---
        CommandTest(
            "Pause the kitchen speaker",
            "music",
            {"action": "pause", "player": "kitchen"},
            "Pause specific player"
        ),
    ]
    tests.extend(music_tests)

    return tests

def _maybe_parse_list_string(value: Any) -> Optional[list]:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not (stripped.startswith("[") and stripped.endswith("]")):
        return None
    try:
        parsed = json.loads(stripped)
    except (json.JSONDecodeError, ValueError, TypeError):
        try:
            parsed = ast.literal_eval(stripped)
        except (ValueError, SyntaxError):
            return None
    return parsed if isinstance(parsed, list) else None


def _build_date_key_to_iso_map(date_context: Optional['DateContext']) -> Dict[str, List[str]]:
    """Build a mapping from symbolic date keys to their resolved ISO date strings."""
    if not date_context:
        return {}

    mapping = {}

    # Single dates
    try:
        mapping["today"] = [date_context.current.date_iso[:10]]
        mapping["tomorrow"] = [date_context.relative_dates.tomorrow.date[:10] if hasattr(date_context.relative_dates.tomorrow, 'date') else date_context.relative_dates.tomorrow.utc_start_of_day[:10]]
        mapping["yesterday"] = [date_context.relative_dates.yesterday.date[:10] if hasattr(date_context.relative_dates.yesterday, 'date') else date_context.relative_dates.yesterday.utc_start_of_day[:10]]
        mapping["day_after_tomorrow"] = [date_context.relative_dates.day_after_tomorrow.date[:10] if hasattr(date_context.relative_dates.day_after_tomorrow, 'date') else date_context.relative_dates.day_after_tomorrow.utc_start_of_day[:10]]
    except (AttributeError, KeyError, TypeError, IndexError):
        pass

    # Weekend dates (lists)
    try:
        if date_context.weekend.this_weekend:
            mapping["this_weekend"] = [d.date[:10] for d in date_context.weekend.this_weekend]
        if date_context.weekend.last_weekend:
            mapping["last_weekend"] = [d.date[:10] for d in date_context.weekend.last_weekend]
        if date_context.weekend.next_weekend:
            mapping["next_weekend"] = [d.date[:10] for d in date_context.weekend.next_weekend]
    except (AttributeError, KeyError, TypeError, IndexError):
        pass

    # Week dates (lists)
    try:
        if date_context.weeks.next_week:
            mapping["next_week"] = [d.date[:10] for d in date_context.weeks.next_week]
        if date_context.weeks.this_week:
            mapping["this_week"] = [d.date[:10] for d in date_context.weeks.this_week]
        if date_context.weeks.last_week:
            mapping["last_week"] = [d.date[:10] for d in date_context.weeks.last_week]
    except (AttributeError, KeyError, TypeError, IndexError):
        pass

    # Year dates (ranges)
    try:
        if date_context.years.this_year:
            mapping["this_year"] = [d.date[:10] for d in date_context.years.this_year]
        if date_context.years.next_year:
            mapping["next_year"] = [d.date[:10] for d in date_context.years.next_year]
        if date_context.years.last_year:
            mapping["last_year"] = [d.date[:10] for d in date_context.years.last_year]
    except (AttributeError, KeyError, TypeError, IndexError):
        pass

    # Month dates (ranges)
    try:
        if date_context.months.this_month:
            mapping["this_month"] = [d.date[:10] for d in date_context.months.this_month]
        if date_context.months.next_month:
            mapping["next_month"] = [d.date[:10] for d in date_context.months.next_month]
        if date_context.months.last_month:
            mapping["last_month"] = [d.date[:10] for d in date_context.months.last_month]
    except (AttributeError, KeyError, TypeError, IndexError):
        pass

    # Weekday dates
    try:
        mapping["next_monday"] = [date_context.weekdays.next_monday.date[:10]]
        mapping["next_tuesday"] = [date_context.weekdays.next_tuesday.date[:10]]
        mapping["next_wednesday"] = [date_context.weekdays.next_wednesday.date[:10]]
        mapping["next_thursday"] = [date_context.weekdays.next_thursday.date[:10]]
        mapping["next_friday"] = [date_context.weekdays.next_friday.date[:10]]
        mapping["next_saturday"] = [date_context.weekdays.next_saturday.date[:10]]
        mapping["next_sunday"] = [date_context.weekdays.next_sunday.date[:10]]
    except (AttributeError, KeyError, TypeError, IndexError):
        pass

    # Time-of-day variants map to same date as their base
    try:
        today_date = date_context.current.date_iso[:10]
        tomorrow_date = mapping.get("tomorrow", [today_date])[0]
        yesterday_date = mapping.get("yesterday", [today_date])[0]

        mapping["morning"] = [today_date]
        mapping["tonight"] = [today_date]
        mapping["last_night"] = [yesterday_date]
        mapping["tomorrow_night"] = [tomorrow_date]
        mapping["tomorrow_morning"] = [tomorrow_date]
        mapping["tomorrow_afternoon"] = [tomorrow_date]
        mapping["tomorrow_evening"] = [tomorrow_date]
        mapping["yesterday_morning"] = [yesterday_date]
        mapping["yesterday_afternoon"] = [yesterday_date]
        mapping["yesterday_evening"] = [yesterday_date]
    except (AttributeError, KeyError, TypeError, IndexError):
        pass

    return mapping


def _values_equal_numeric(expected: Any, actual: Any) -> bool:
    """Check if two values are numerically equal, handling string/number mismatches."""
    try:
        return float(expected) == float(actual)
    except (ValueError, TypeError):
        return False


def _normalize_datetime_value(value: Any, date_key_map: Optional[Dict[str, List[str]]] = None, local_tz: Optional[Any] = None) -> Optional[str]:
    """
    Normalize a datetime value to a local date string (YYYY-MM-DD).

    If local_tz is provided (ZoneInfo or datetime.timezone), UTC timestamps are
    converted to local time before extracting the date. This is important because
    2026-01-29T01:00:00Z (1am UTC) is actually 2026-01-28T20:00:00 EST (8pm previous day).
    """
    # Default to EST (UTC-5) if no timezone provided - matches typical user timezone
    if local_tz is None:
        try:
            local_tz = ZoneInfo("America/New_York")
        except Exception:
            local_tz = datetime.timezone(datetime.timedelta(hours=-5))  # noqa: DTZ001

    if isinstance(value, datetime.datetime):
        if value.tzinfo is not None:
            # Convert to local timezone before extracting date
            local_dt = value.astimezone(local_tz)
            return local_dt.date().isoformat()
        return value.date().isoformat()
    if isinstance(value, datetime.date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    stripped = value.strip().lower()
    # If it's a symbolic key and we have a mapping, resolve it
    if stripped in ALL_DATE_KEYS and date_key_map and stripped in date_key_map:
        # Return the first date for single-date keys
        return date_key_map[stripped][0] if date_key_map[stripped] else None
    # Don't just take first 10 chars - need to parse and convert timezone
    try:
        if stripped.endswith("z"):
            stripped = f"{stripped[:-1]}+00:00"
        parsed = datetime.datetime.fromisoformat(stripped)
        if parsed.tzinfo is not None:
            # Convert UTC to local timezone before extracting date
            local_dt = parsed.astimezone(local_tz)
            return local_dt.date().isoformat()
        return parsed.date().isoformat()
    except (ValueError, TypeError, AttributeError):
        pass
    # Fallback: if it looks like a date string, extract it
    if len(stripped) >= 10 and stripped[4:5] == "-" and stripped[7:8] == "-":
        return stripped[:10]
    return None


def _normalize_datetimes_list(value: Any, date_key_map: Optional[Dict[str, List[str]]] = None, local_tz: Optional[datetime.timezone] = None) -> Optional[List[str]]:
    if not isinstance(value, list):
        return None
    normalized = []
    for item in value:
        # For symbolic keys that represent multiple dates, expand them
        if isinstance(item, str) and item.strip().lower() in (date_key_map or {}):
            key = item.strip().lower()
            expanded = date_key_map[key]
            normalized.extend(expanded)
        else:
            normalized_item = _normalize_datetime_value(item, date_key_map, local_tz)
            if not normalized_item:
                return None
            normalized.append(normalized_item)
    return sorted(normalized) if normalized else None


def _dates_match_flexibly(expected_dates: List[str], actual_dates: List[str]) -> bool:
    """
    Flexibly match date lists. Returns True if:
    1. They match exactly, OR
    2. The actual dates are a subset that includes the START of the expected range, OR
    3. The expected dates are a subset of actual (model returned extra dates), OR
    4. For multi-date ranges, at least half the expected dates overlap with actual

    This allows accepting a single start-of-range date when the expected is a full range
    (e.g., accepting ["2026-01-31"] when expected is ["2026-01-31", "2026-02-01"] for "this_weekend")
    """
    if expected_dates == actual_dates:
        return True

    # If actual is a single date and expected is a range, check if actual is the range start
    if len(actual_dates) == 1 and len(expected_dates) > 1:
        # Accept if the single actual date is the START of the expected range
        if actual_dates[0] == expected_dates[0]:
            return True
        # Also accept if actual date falls within the expected range
        if actual_dates[0] in expected_dates:
            return True

    # If actual has multiple dates, all must be in expected
    if set(actual_dates).issubset(set(expected_dates)):
        return True

    # Reverse subset: expected dates are all in actual (model returned extra dates)
    if set(expected_dates).issubset(set(actual_dates)):
        return True

    # For multi-date ranges, accept if majority overlap
    if len(expected_dates) > 1 and len(actual_dates) > 1:
        overlap = set(expected_dates) & set(actual_dates)
        if len(overlap) >= len(expected_dates) / 2:
            return True

    return False


def _normalize_tool_call(tool_call: Any) -> Optional[dict]:
    if not isinstance(tool_call, dict):
        return None
    name = tool_call.get("name")
    arguments = tool_call.get("arguments", {})
    if not name and "function" in tool_call:
        function = tool_call.get("function") or {}
        name = function.get("name") or name
        arguments = function.get("arguments", arguments)
    if not name:
        return None
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return None
    return {
        "command_name": name,
        "parameters": arguments
    }


def _extract_tool_call_from_assistant_message(response: Any, response_dict: Optional[dict]) -> Optional[dict]:
    message = None
    if hasattr(response, "assistant_message"):
        message = response.assistant_message
    elif hasattr(response, "message"):
        message = response.message
    if message is None and isinstance(response_dict, dict):
        message = response_dict.get("assistant_message") or response_dict.get("message")
    if not message or not isinstance(message, str):
        return None
    try:
        parsed = json.loads(message.strip())
    except (json.JSONDecodeError, ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    tool_call = parsed.get("tool_call")
    if tool_call:
        return _normalize_tool_call(tool_call)
    tool_calls = parsed.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return _normalize_tool_call(tool_calls[0])
    return None


def evaluate_parse_result(
    result: 'ParseResult',
    test: CommandTest,
    date_context: DateContext,
    test_index: int,
    service: Optional[Any] = None,
    available_commands: Optional[Dict] = None,
    validate: bool = False,
) -> tuple[bool, str]:
    """Evaluate a ParseResult against expected test outcomes.

    Args:
        result: ParseResult from CommandExecutionService.parse_voice_command()
        test: The test case with expected command/params
        date_context: Date context for date normalization
        test_index: Index of this test in the suite
        service: CommandExecutionService instance (needed for --validate retry)
        available_commands: Dict of command_name -> IJarvisCommand instances (for validation)
        validate: If True, run validate_call() on returned tool_calls

    Returns:
        tuple: (success: bool, failure_reason: str)
    """

    print(f"\n🧪 Testing: {test.description}")
    print(f"   Voice Command: '{test.voice_command}'")
    print(f"   Expected Command: {test.expected_command}")
    print(f"   Expected Params: {test.expected_params}")

    try:
        if not result.success and result.tool_name is None:
            # Pre-route or CC failure
            failure_reason = result.assistant_message or "Parse failed with no tool call"
            print(f"   ❌ {failure_reason}")
            return False, failure_reason

        # Build command_response dict for comparison
        actual_command = result.tool_name
        actual_params = dict(result.tool_arguments) if result.tool_arguments else {}

        if result.pre_routed:
            print(f"   ⚡ Pre-routed to: {actual_command} {actual_params}")
        elif result.raw_response:
            response_dict = result.raw_response.model_dump() if hasattr(result.raw_response, 'model_dump') else {}
            print(f"   📡 Response received: {json.dumps(response_dict, indent=2, default=str)}")

        # Handle case where no tool call was produced
        if actual_command is None:
            # Check for tool call embedded in assistant message
            if result.raw_response:
                response_dict = result.raw_response.model_dump() if hasattr(result.raw_response, 'model_dump') else {}
                parsed_tool_call = _extract_tool_call_from_assistant_message(result.raw_response, response_dict)
                if parsed_tool_call:
                    print(f"   🔧 Tool call parsed from assistant_message")
                    actual_command = parsed_tool_call["command_name"]
                    actual_params = parsed_tool_call["parameters"]

            if actual_command is None:
                # Direct completion (no tool call)
                if result.raw_response and getattr(result.raw_response, "stop_reason", None) == "complete":
                    direct_completion_whitelist = {"calculate", "answer_question", "tell_joke"}
                    if test.expected_command in direct_completion_whitelist:
                        print(f"   ℹ️ No tool calls; treating direct completion as acceptable.")
                        return True, ""
                    failure_reason = "Direct completion not allowed for this command"
                    print(f"   ❌ {failure_reason}")
                    return False, failure_reason
                failure_reason = "No tool_calls or commands in response"
                print(f"   ❌ {failure_reason}")
                return False, failure_reason

        # If the raw response has multiple tool calls, prefer the one matching expected
        if not result.pre_routed and result.raw_response and result.raw_response.tool_calls:
            for tc in result.raw_response.tool_calls:
                if tc.function.name == test.expected_command:
                    actual_command = tc.function.name
                    actual_params = tc.function.get_arguments_dict()
                    # Re-apply post-processing on the matched tool call
                    if service:
                        cmd = service.command_discovery.get_command(actual_command)
                        if cmd:
                            actual_params = cmd.post_process_tool_call(actual_params, test.voice_command)
                    break

        command_response = {"command_name": actual_command, "parameters": actual_params}

        # --- Node-side validate_call() simulation ---
        if validate and available_commands and command_response["command_name"] in available_commands:
            cmd_instance = available_commands[command_response["command_name"]]
            cmd_name = command_response["command_name"]

            # Monkey-patch _get_known_entities for HA commands so validation
            # uses the test's MOCK_HA_CONTEXT instead of the (absent) agent scheduler.
            original_get_entities = None
            if test.ha_context and cmd_name in ("control_device", "get_device_status"):
                mock_entities = _entities_from_ha_context(test.ha_context)
                from commands.control_device.command import ControlDeviceCommand
                original_get_entities = ControlDeviceCommand._get_known_entities
                ControlDeviceCommand._get_known_entities = staticmethod(lambda: mock_entities)

            try:
                v_results = cmd_instance.validate_call(**command_response["parameters"])
            finally:
                if original_get_entities is not None:
                    from commands.control_device.command import ControlDeviceCommand
                    ControlDeviceCommand._get_known_entities = original_get_entities

            # Apply auto-corrections
            corrections = [r for r in v_results if r.suggested_value is not None]
            for r in corrections:
                old_val = command_response["parameters"].get(r.param_name)
                command_response["parameters"][r.param_name] = r.suggested_value
                print(f"   🔧 Auto-corrected: {r.param_name} '{old_val}' -> '{r.suggested_value}'")

            # Handle validation errors — send back to CC for LLM retry
            errors = [r for r in v_results if not r.success]
            if errors and service:
                tool_call_id = "validation"
                if result.raw_response and result.raw_response.tool_calls:
                    tool_call_id = result.raw_response.tool_calls[0].id

                error_text = "\n".join([r.message for r in errors if r.message])
                print(f"   🔄 Validation failed ({len(errors)} error(s)), sending back to CC for retry...")
                for e in errors:
                    msg_preview = (e.message or "")[:100]
                    print(f"      ⚠️  {e.param_name}: {msg_preview}")

                retry_response = service.client.send_tool_results(result.conversation_id, [{
                    "tool_call_id": tool_call_id,
                    "output": json.dumps({"success": False, "error": error_text}),
                }])

                if retry_response and hasattr(retry_response, 'tool_calls') and retry_response.tool_calls:
                    retry_tool = retry_response.tool_calls[0]
                    command_response = {
                        "command_name": retry_tool.function.name,
                        "parameters": retry_tool.function.get_arguments_dict(),
                    }
                    print(f"   🔄 Retry produced: {command_response['command_name']} {command_response['parameters']}")
                else:
                    failure_reason = "Validation retry did not produce corrected tool_calls"
                    print(f"   ❌ {failure_reason}")
                    return False, failure_reason

        # Check command name (with leniency for certain search/web misroutes)
        actual_command = command_response["command_name"]
        if actual_command != test.expected_command:
            # Accept close web/search variants if query matches intended search AND required params exist.
            # quick_search + deep_research are CC built-in search tools that coexist with Pantry search_web —
            # model legitimately picks either for a generic web query.
            if test.expected_command == "search_web" and actual_command in {
                "get_weather", "get_sports_schedule", "get_sports", "get_web_search_results",
                "quick_search", "deep_research",
            }:
                act_params = command_response["parameters"]

                # Special case: sports command with empty team_name means validation
                # would re-route to search_web. Accept as valid without --validate.
                if actual_command in {"get_sports", "get_sports_schedule"}:
                    team = act_params.get("team_name", "")
                    if not team or not str(team).strip():
                        print(f"   ⚠️  Command mismatch accepted: {actual_command} with empty team_name (validation would re-route)")
                        return True, ""

                exp_query = test.expected_params.get("query")
                act_query = act_params.get("query")

                # Validate search overlap
                overlap_ok = exp_query and act_query and is_valid_search_query(exp_query, act_query)

                # For sports/weather fallbacks, ensure required params are populated (avoid empty team/date)
                params_ok = True
                if actual_command in {"get_sports", "get_sports_schedule"}:
                    team = act_params.get("team_name")
                    dates = act_params.get("resolved_datetimes")
                    params_ok = bool(team) and isinstance(dates, list) and len(dates) > 0
                elif actual_command == "get_weather":
                    city = act_params.get("city")
                    params_ok = bool(city)

                if overlap_ok and params_ok:
                    print(f"   ⚠️  Command mismatch allowed: expected search_web, got {actual_command} with acceptable query/params")
                else:
                    failure_reason = f"Command mismatch: expected '{test.expected_command}', got '{actual_command}'"
                    print(f"   ❌ {failure_reason}")
                    return False, failure_reason
            else:
                failure_reason = f"Command mismatch: expected '{test.expected_command}', got '{actual_command}'"
                print(f"   ❌ {failure_reason}")
                return False, failure_reason

        print(f"   ✅ Command name matches: {actual_command}")

        # Check parameters
        actual_params = command_response["parameters"]
        missing_params = []
        mismatched_params = []

        # Build date key map for resolving symbolic dates to ISO dates
        date_key_map = _build_date_key_to_iso_map(date_context)

        # Get user's local timezone for proper UTC->local conversion
        local_tz = None
        try:
            if date_context and date_context.timezone and date_context.timezone.user_timezone:
                local_tz = ZoneInfo(date_context.timezone.user_timezone)
        except (KeyError, AttributeError, ValueError):
            pass  # Will fall back to EST default in normalization

        print(f"   🔍 Debug: Test index = {test_index}")

        for expected_key, expected_value in test.expected_params.items():
            if expected_key not in actual_params:
                # resolved_datetimes defaults to ["today"] server-side, so missing is OK
                if expected_key == "resolved_datetimes" and expected_value == ["today"]:
                    print(f"   ⚠️  Test {test_index}: resolved_datetimes missing but defaults to today server-side")
                    continue
                # entity_id ↔ device_name: shipping control_device tool accepts either;
                # match entity_id's short form against actual device_name substring.
                if expected_key == "entity_id" and "device_name" in actual_params:
                    short = str(expected_value).split(".", 1)[-1].replace("_", " ").lower()
                    device_name_val = str(actual_params["device_name"]).lower().replace("_", " ").replace(".", " ")
                    if short in device_name_val or device_name_val in short:
                        print(f"   ⚠️  Test {test_index}: entity_id matched via device_name ({expected_value} ≈ {actual_params['device_name']})")
                        continue
                missing_params.append(expected_key)
            else:
                actual_value = actual_params[expected_key]
                if isinstance(expected_value, list):
                    parsed_list = _maybe_parse_list_string(actual_value)
                    if parsed_list is not None:
                        actual_value = parsed_list
                if expected_key == "resolved_datetimes":
                    normalized_expected = _normalize_datetimes_list(expected_value, date_key_map, local_tz)
                    normalized_actual = _normalize_datetimes_list(actual_value, date_key_map, local_tz)
                    if normalized_expected is not None and normalized_actual is not None:
                        if _dates_match_flexibly(normalized_expected, normalized_actual):
                            if normalized_expected == normalized_actual:
                                print(f"   ⚠️  Test {test_index}: Datetimes matched by date-only comparison")
                            else:
                                print(f"   ⚠️  Test {test_index}: Datetimes matched flexibly (subset/range-start accepted)")
                            continue
                if actual_value == expected_value:
                    continue
                # Special handling for query parameters - normalize punctuation and case and allow semantic overlap
                elif expected_key == "query" and isinstance(expected_value, str) and isinstance(actual_value, str):
                    import string
                    expected_normalized = expected_value.translate(str.maketrans('', '', string.punctuation)).lower().strip()
                    actual_normalized = actual_value.translate(str.maketrans('', '', string.punctuation)).lower().strip()

                    if expected_normalized == actual_normalized:
                        print(f"   ⚠️  Test {test_index}: Query validation passed (punctuation/case normalized)")
                        continue
                    # Allow looser semantic match for knowledge/search-style questions
                    if is_valid_search_query(expected_value, actual_value):
                        print(f"   ⚠️  Test {test_index}: Query validation passed (semantic overlap)")
                        continue
                    else:
                        mismatched_params.append(f"{expected_key}: expected {expected_value}, got {actual_value}")
                # Special handling for team names - allow either short name or full name
                elif expected_key == "team_name" and isinstance(expected_value, str) and isinstance(actual_value, str):
                    expected_lower = expected_value.lower()
                    actual_lower = actual_value.lower()

                    # Check if the expected team name is contained in the actual (e.g., "Yankees" in "New York Yankees")
                    # or if they're exactly equal
                    if expected_lower == actual_lower or expected_lower in actual_lower or actual_lower in expected_lower:
                        print(f"   ⚠️  Test {test_index}: Team name validation passed (flexible matching)")
                        continue
                    else:
                        mismatched_params.append(f"{expected_key}: expected {expected_value}, got {actual_value}")
                # Special handling for numeric parameters - allow string/int comparison
                elif expected_key in ("num1", "num2", "value", "duration", "duration_seconds", "hours", "minutes", "seconds") and _values_equal_numeric(expected_value, actual_value):
                    print(f"   ⚠️  Test {test_index}: Numeric value matched (type-normalized)")
                    continue
                # HA action aliases: open/open_cover, close/close_cover are equivalent for the shipping tool
                elif expected_key == "action" and isinstance(expected_value, str) and isinstance(actual_value, str):
                    _ACTION_ALIASES = {
                        "open_cover": "open", "close_cover": "close",
                        "lock": "lock", "unlock": "unlock",
                    }
                    if _ACTION_ALIASES.get(expected_value) == actual_value or _ACTION_ALIASES.get(actual_value) == expected_value:
                        print(f"   ⚠️  Test {test_index}: Action alias matched ({expected_value} ≈ {actual_value})")
                        continue
                    else:
                        mismatched_params.append(f"{expected_key}: expected {expected_value}, got {actual_value}")
                else:
                    mismatched_params.append(f"{expected_key}: expected {expected_value}, got {actual_value}")

        if missing_params or mismatched_params:
            failure_reason = "Parameter validation failed: "
            if missing_params:
                failure_reason += f"Missing: {', '.join(missing_params)}. "
            if mismatched_params:
                failure_reason += f"Mismatched: {'; '.join(mismatched_params)}"
            print(f"   ❌ {failure_reason}")
            return False, failure_reason

        print(f"   ✅ All expected parameters match")
        return True, ""

    except Exception as e:
        failure_reason = f"Exception during test: {str(e)}"
        print(f"   ❌ {failure_reason}")
        import traceback
        traceback.print_exc()
        return False, failure_reason

def is_valid_search_query(expected_query, actual_query):
    """
    Check if the actual search query contains the key concepts from the expected query.
    This allows for LLM optimization of search terms while ensuring core concepts are preserved.
    """
    if not expected_query or not actual_query:
        return False
    
    # Convert to lowercase for comparison
    expected_lower = expected_query.lower()
    actual_lower = actual_query.lower()
    
    # If they're exactly the same, that's perfect
    if expected_lower == actual_lower:
        return True
    
    # Expanded stop words - remove articles, question words, common verbs, etc.
    stop_words = {
        # Articles and determiners
        'the', 'a', 'an', 'this', 'that', 'these', 'those',
        # Conjunctions and prepositions  
        'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by', 'from', 'about',
        # Question words and verbs
        'what', 'when', 'where', 'who', 'how', 'why', 'which',
        'is', 'are', 'was', 'were', 'do', 'does', 'did', 'have', 'has', 'had',
        'will', 'would', 'could', 'should', 'can', 'may', 'might',
        # Time/search related words that don't add search value
        'latest', 'current', 'recent', 'today', 'now', 'find', 'search', 'get', 'show', 'tell', 'news', 'information',
        # Common filler words
        'me', 'my', 'i', 'you', 'it', 'they', 'them', 'their', 'there', 'here'
    }
    
    import re
    expected_words = set(re.findall(r'\b\w+\b', expected_lower))
    actual_words = set(re.findall(r'\b\w+\b', actual_lower))
    
    # Remove stop words to focus on core concepts
    key_expected_words = expected_words - stop_words
    key_actual_words = actual_words - stop_words
    
    # Check if most key concepts are preserved
    if not key_expected_words:
        return True  # If no key words after filtering, accept anything

    # Stem-aware overlap: "vaccines"→"vaccine", "running"→"run", etc.
    def _stem(w: str) -> str:
        for suffix in ("es", "s", "ing", "ed", "tion", "ment"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                return w[: -len(suffix)]
        return w

    stemmed_expected = {_stem(w) for w in key_expected_words}
    stemmed_actual = {_stem(w) for w in key_actual_words}

    # Prefix-aware overlap: "president" ↔ "presidential", "lead" ↔ "leader" should match.
    # A stemmed expected word matches if any stemmed actual shares a common prefix ≥4 chars.
    matched = 0
    for exp_stem in stemmed_expected:
        if exp_stem in stemmed_actual:
            matched += 1
            continue
        for act_stem in stemmed_actual:
            common_prefix = 0
            for a, b in zip(exp_stem, act_stem):
                if a == b:
                    common_prefix += 1
                else:
                    break
            if common_prefix >= 4 and common_prefix >= min(len(exp_stem), len(act_stem)) * 0.7:
                matched += 1
                break
    coverage = matched / len(stemmed_expected)

    # 40% overlap acceptable — LLM aggressively rephrases queries, prefix-aware helps
    return coverage >= 0.4


def write_results_to_file(filename: str, results: dict):
    """Write test results to a JSON file"""
    try:
        with open(filename, 'w') as f:
            json.dump(results, f, indent=2, default=str)
    except Exception as e:
        print(f"❌ Failed to write results to file: {e}")


def generate_analysis(test_results: list) -> dict:
    """Generate analysis of test results to identify patterns"""
    
    # Command confusion matrix - which commands are confused with each other
    command_confusion = {}
    
    # Parameter extraction issues
    param_issues = {}
    
    # Command success rates
    command_accuracy = {}
    
    for result in test_results:
        expected_cmd = result['expected']['command']
        actual_cmd = result['actual']['command']
        passed = result['passed']
        
        # Track command accuracy
        if expected_cmd not in command_accuracy:
            command_accuracy[expected_cmd] = {"total": 0, "correct": 0}
        command_accuracy[expected_cmd]["total"] += 1
        if passed:
            command_accuracy[expected_cmd]["correct"] += 1
        
        # Track command confusion (wrong command selected)
        if not passed and actual_cmd and actual_cmd != expected_cmd:
            key = f"{expected_cmd} → {actual_cmd}"
            command_confusion[key] = command_confusion.get(key, 0) + 1
        
        # Track parameter extraction issues (right command, wrong params)
        if not passed and actual_cmd == expected_cmd:
            if expected_cmd not in param_issues:
                param_issues[expected_cmd] = []
            param_issues[expected_cmd].append({
                "voice_command": result['voice_command'],
                "expected_params": result['expected']['parameters'],
                "actual_params": result['actual']['parameters'],
                "failure_reason": result['failure_reason']
            })
    
    # Calculate success rates per command
    command_success_rates = {}
    for cmd, stats in command_accuracy.items():
        rate = round((stats["correct"] / stats["total"] * 100), 2) if stats["total"] > 0 else 0
        command_success_rates[cmd] = {
            "success_rate": rate,
            "passed": stats["correct"],
            "failed": stats["total"] - stats["correct"],
            "total": stats["total"]
        }
    
    # Sort by success rate (lowest first to highlight problems)
    sorted_success_rates = dict(sorted(command_success_rates.items(), key=lambda x: x[1]['success_rate']))
    
    # Sort confusion matrix by frequency (most common confusions first)
    sorted_confusion = dict(sorted(command_confusion.items(), key=lambda x: x[1], reverse=True))
    
    return {
        "command_success_rates": sorted_success_rates,
        "command_confusion_matrix": sorted_confusion,
        "parameter_extraction_issues": param_issues,
        "recommendations": generate_recommendations(sorted_success_rates, sorted_confusion, param_issues)
    }


def generate_recommendations(success_rates: dict, confusion: dict, param_issues: dict) -> list:
    """Generate actionable recommendations based on analysis"""
    recommendations = []
    
    # Recommend improving low-performing commands
    for cmd, stats in success_rates.items():
        if stats['success_rate'] < 70:
            recommendations.append({
                "priority": "HIGH",
                "command": cmd,
                "issue": f"Low success rate: {stats['success_rate']}%",
                "suggestion": f"Review and improve command description. Consider adding more specific use cases and anti-patterns to distinguish from similar commands."
            })
    
    # Recommend addressing common confusions
    for confusion_pair, count in list(confusion.items())[:5]:  # Top 5 confusions
        expected, actual = confusion_pair.split(' → ')
        recommendations.append({
            "priority": "MEDIUM",
            "command": expected,
            "issue": f"Confused with '{actual}' {count} time(s)",
            "suggestion": f"Add explicit anti-pattern in '{expected}' description: 'Do NOT use for [actual command use case]. Use {actual} instead.'"
        })
    
    # Recommend parameter improvements
    for cmd, issues in param_issues.items():
        if len(issues) > 2:  # More than 2 parameter issues
            recommendations.append({
                "priority": "MEDIUM",
                "command": cmd,
                "issue": f"{len(issues)} parameter extraction failures",
                "suggestion": "Review parameter descriptions and add more inline examples of valid values and formats."
            })
    
    return recommendations


def list_tests_only():
    """List all tests without importing the full command stack"""
    print("📋 AVAILABLE TESTS:")
    print("=" * 80)
    
    # Create test commands with minimal fallback data for listing
    # Note: This is just for listing, not for actual execution
    try:
        # Try to get real date context first
        from clients.jarvis_command_center_client import JarvisCommandCenterClient
        from utils.config_loader import Config
        
        jcc_url = Config.get("jarvis_command_center_api_url")
        if jcc_url:
            jcc_client = JarvisCommandCenterClient(jcc_url)
            date_context = jcc_client.get_date_context()
            if date_context:
                test_commands = create_test_commands_with_context(date_context)
            else:
                print("⚠️  Could not get date context from server, using fallback data")
                test_commands = create_test_commands()
        else:
            test_commands = create_test_commands()
    except Exception as e:
        print(f"⚠️  Using fallback data due to error: {e}")
        test_commands = create_test_commands()
    
    if test_commands:
        for i, test in enumerate(test_commands):
            print(f"Test #{i:2d}: {test.description}")
            print(f"           Voice: '{test.voice_command}'")
            print(f"           Expected: {test.expected_command} -> {test.expected_params}")
            print()
        
        print(f"Total: {len(test_commands)} tests")
    else:
        print("❌ No tests available")
    
    print("\nTo run specific tests:")
    print("  python3 test_command_parsing.py -t 5 7 11")
    print("  python3 test_command_parsing.py -t 5")
    print("  python3 test_command_parsing.py -c calculator_command")

def main():
    """Main function to run all command parsing tests"""
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='Test command parsing for Jarvis commands')
    parser.add_argument('--test-indices', '-t', nargs='+', type=int, 
                       help='Run only specific tests by index (0-based). Example: -t 5 7 11')
    parser.add_argument('--list-tests', '-l', action='store_true',
                       help='List all available tests with their indices')
    parser.add_argument('--command', '-c', nargs='+', type=str,
                       help='Run only tests for specific commands. Example: -c calculator_command sports_score_command')
    parser.add_argument('--output', '-o', type=str, default='test_results.json',
                       help='Output file for test results (default: test_results.json)')
    parser.add_argument('--validate', '-v', action='store_true',
                       help='Run node-side validate_call() on returned tool_calls (simulates production validation + retry)')
    parser.add_argument('--adapter-hash', type=str, default=None,
                       help='Server-side adapter hash to use for this eval run. '
                            'Command Center honors this only when JARVIS_TEST_MODE=1 is set.')
    parser.add_argument('--adapter-scale', type=float, default=1.0,
                       help='LoRA scale to apply when --adapter-hash is set (default: 1.0)')
    parser.add_argument('--no-adapter', action='store_true',
                       help='Force a baseline run (no adapter even if household has one deployed). '
                            'Honored only when JARVIS_TEST_MODE=1 is set on the server.')
    args = parser.parse_args()

    # Build adapter_settings payload once (None when not specified)
    adapter_settings: dict | None = None
    if args.no_adapter:
        adapter_settings = {"hash": "", "scale": 1.0, "enabled": False}
        print("🧪 Eval adapter override: BASELINE (no adapter)")
    elif args.adapter_hash:
        adapter_settings = {
            "hash": args.adapter_hash,
            "scale": float(args.adapter_scale),
            "enabled": True,
        }
        print(f"🧪 Eval adapter override: hash={args.adapter_hash[:12]}… scale={args.adapter_scale}")
    
    # Create test commands with real date context
    test_commands = create_test_commands()

    # List tests if requested
    if args.list_tests:
        list_tests_only()
        return

    # Only import heavy modules when actually running tests
    from utils.command_execution_service import CommandExecutionService

    # Initialize the shared service (discovers commands, connects to CC)
    try:
        service = CommandExecutionService()
        print(f"✅ Connected to Command Center at: {service.command_center_url}")
    except Exception as e:
        print(f"❌ Failed to initialize CommandExecutionService: {e}")
        return

    # Get available commands
    available_commands = service.command_discovery.get_all_commands()

    if not available_commands:
        print("❌ No commands found. Make sure the command discovery service is working.")
        return

    print(f"✅ Found {len(available_commands)} commands:")
    for cmd in available_commands.values():
        print(f"   - {cmd.command_name}: {cmd.description}")

    # Get real date context from server
    try:
        date_context = service.client.get_date_context()
        if date_context:
            print(f"✅ Got real date context from server for timezone: {date_context.timezone.user_timezone}")
            # Recreate test commands with real date context
            test_commands = create_test_commands_with_context(date_context)
        else:
            print("⚠️  Could not get date context from server, using fallback")
            test_commands = create_test_commands()

    except Exception as e:
        print(f"❌ Failed to get date context: {e}")
        return
    
    # Filter tests if specific indices provided
    if args.test_indices:
        filtered_tests = []
        for idx in args.test_indices:
            if 0 <= idx < len(test_commands):
                filtered_tests.append((idx, test_commands[idx]))
            else:
                print(f"⚠️  Test index {idx} is out of range (0-{len(test_commands)-1})")
        
        if not filtered_tests:
            print("❌ No valid test indices provided")
            return
            
        print(f"🎯 Running {len(filtered_tests)} selected tests: {args.test_indices}")
        test_commands_to_run = filtered_tests
    elif args.command:
        # Filter tests by command name
        filtered_tests = []
        for i, test in enumerate(test_commands):
            if test.expected_command in args.command:
                filtered_tests.append((i, test))
        
        if not filtered_tests:
            print(f"❌ No tests found for commands: {args.command}")
            print(f"Available commands: {list(set(test.expected_command for test in test_commands))}")
            return
            
        print(f"🎯 Running {len(filtered_tests)} tests for commands: {args.command}")
        test_commands_to_run = filtered_tests
    else:
        print(f"🚀 Running all {len(test_commands)} tests...")
        test_commands_to_run = [(i, test) for i, test in enumerate(test_commands)]
    
    # Run all tests
    if args.validate:
        print(f"\n🧪 Running {len(test_commands_to_run)} command parsing tests (with node-side validation)...")
    else:
        print(f"\n🧪 Running {len(test_commands_to_run)} command parsing tests...")
    print("=" * 60)
    
    passed_tests = 0
    failed_tests = 0
    all_test_results = []  # Store ALL test results for file output
    failed_test_details = []
    response_times = []
    for i, test in test_commands_to_run:
        print(f"\n📝 Test {i}/{len(test_commands_to_run)}")

        try:
            # Use the shared production code path: pre-route → register → send → post-process
            start_time = time.time()
            result = service.parse_voice_command(
                test.voice_command,
                agents=test.ha_context if test.ha_context else None,
                warmup_delay=1.5,
                adapter_settings=adapter_settings,
            )
            response_time = time.time() - start_time
            response_times.append(response_time)

            # Evaluate the result against expectations
            test_success, failure_reason = evaluate_parse_result(
                result, test, date_context, i,
                service=service,
                available_commands=available_commands,
                validate=args.validate,
            )

            # Build response dict for reporting
            raw_response_dict = result.raw_response.model_dump() if result.raw_response and hasattr(result.raw_response, 'model_dump') else None

            # Store comprehensive test result
            test_result = {
                "test_number": i,
                "passed": test_success,
                "description": test.description,
                "voice_command": test.voice_command,
                "expected": {
                    "command": test.expected_command,
                    "parameters": test.expected_params
                },
                "actual": {
                    "command": result.tool_name,
                    "parameters": result.tool_arguments
                },
                "pre_routed": result.pre_routed,
                "response_time_seconds": round(response_time, 3),
                "conversation_id": result.conversation_id,
                "failure_reason": failure_reason if not test_success else None,
                "full_response": raw_response_dict
            }
            all_test_results.append(test_result)

            if test_success:
                passed_tests += 1
                pre_tag = " [pre-routed]" if result.pre_routed else ""
                print(f"   ✅ Test PASSED{pre_tag} (⏱️  {response_time:.2f}s)")
            else:
                failed_tests += 1
                print(f"   ❌ Test FAILED (⏱️  {response_time:.2f}s)")
                failed_test_details.append({
                    "test_number": i,
                    "description": test.description,
                    "voice_command": test.voice_command,
                    "expected_command": test.expected_command,
                    "expected_params": test.expected_params,
                    "failure_reason": failure_reason,
                    "actual_response": raw_response_dict,
                    "conversation_id": result.conversation_id
                })

        except Exception as e:
            print(e)
            print(f"❌ Error during test {i}: {e}")
            failed_tests += 1
            error_result = {
                "test_number": i,
                "passed": False,
                "description": test.description,
                "voice_command": test.voice_command,
                "expected": {
                    "command": test.expected_command,
                    "parameters": test.expected_params
                },
                "actual": {
                    "command": None,
                    "parameters": None
                },
                "pre_routed": False,
                "response_time_seconds": 0,
                "conversation_id": "",
                "failure_reason": f"Exception: {str(e)}",
                "full_response": None
            }
            all_test_results.append(error_result)
            failed_test_details.append({
                "test_number": i,
                "description": test.description,
                "voice_command": test.voice_command,
                "error": str(e),
                "conversation_id": ""
            })
    
    # Print summary
    print(f"\n" + "=" * 60)
    print(f"📊 TEST SUMMARY")
    print(f"   Total Tests: {len(test_commands_to_run)}")
    print(f"   Passed: {passed_tests}")
    print(f"   Failed: {failed_tests}")
    print(f"   Success Rate: {(passed_tests/len(test_commands_to_run)*100):.1f}%")
    
    # Performance metrics
    if response_times:
        avg_response_time = sum(response_times) / len(response_times)
        min_response_time = min(response_times)
        max_response_time = max(response_times)
        print(f"\n⏱️  PERFORMANCE METRICS")
        print(f"   Average Response Time: {avg_response_time:.2f}s")
        print(f"   Min Response Time: {min_response_time:.2f}s")
        print(f"   Max Response Time: {max_response_time:.2f}s")
        print(f"   Total Test Time: {sum(response_times):.2f}s")
    
    # Failure summary
    if failed_test_details:
        print(f"\n❌ FAILED TEST DETAILS")
        print(f"   " + "=" * 50)
        for failure in failed_test_details:
            print(f"   Test #{failure['test_number']}: {failure['description']}")
            print(f"      Voice Command: '{failure['voice_command']}'")
            print(f"      Conversation ID: {failure.get('conversation_id', 'N/A')}")
            if 'expected_command' in failure:
                print(f"      Expected Command: {failure['expected_command']}")
                print(f"      Expected Params: {failure['expected_params']}")
                if 'failure_reason' in failure:
                    print(f"      Failure Reason: {failure['failure_reason']}")
                if 'actual_response' in failure and failure['actual_response']:
                    print(f"      📡 ACTUAL RESPONSE:")
                    if 'commands' in failure['actual_response'] and failure['actual_response']['commands']:
                        actual_cmd = failure['actual_response']['commands'][0]
                        print(f"         Command: {actual_cmd.get('command_name', 'MISSING')}")
                        print(f"         Parameters: {actual_cmd.get('parameters', 'MISSING')}")
                    else:
                        print(f"         {json.dumps(failure['actual_response'], indent=8)}")
            if 'error' in failure:
                print(f"      Error: {failure['error']}")
            print(f"      " + "-" * 30)
    
    if failed_tests == 0:
        print(f"\n🎉 All tests passed! Command parsing is working correctly.")
    else:
        print(f"\n⚠️  {failed_tests} test(s) failed. Check the details above.")
    
    # Write results to file
    print(f"\n📄 Writing results to {args.output}...")
    write_results_to_file(args.output, {
        "summary": {
            "total_tests": len(test_commands_to_run),
            "passed": passed_tests,
            "failed": failed_tests,
            "success_rate": round((passed_tests / len(test_commands_to_run) * 100), 2) if test_commands_to_run else 0,
            "avg_response_time": round(sum(response_times) / len(response_times), 3) if response_times else 0,
            "min_response_time": round(min(response_times), 3) if response_times else 0,
            "max_response_time": round(max(response_times), 3) if response_times else 0,
            "test_run_timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        },
        "test_results": all_test_results,
        "failed_test_details": failed_test_details,
        "analysis": generate_analysis(all_test_results),
        "slow_tests": []
    })
    print(f"✅ Results written to {args.output}")

    # Also write to JCC temp location for server-side tooling
    jcc_results_path = "/Users/alexanderberardi/jarvis/jarvis-command-center/temp/test_results.json"
    write_results_to_file(jcc_results_path, {
        "summary": {
            "total_tests": len(test_commands_to_run),
            "passed": passed_tests,
            "failed": failed_tests,
            "success_rate": round((passed_tests / len(test_commands_to_run) * 100), 2) if test_commands_to_run else 0,
            "avg_response_time": round(sum(response_times) / len(response_times), 3) if response_times else 0,
            "min_response_time": round(min(response_times), 3) if response_times else 0,
            "max_response_time": round(max(response_times), 3) if response_times else 0,
            "test_run_timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
        },
        "test_results": all_test_results,
        "failed_test_details": failed_test_details,
        "analysis": generate_analysis(all_test_results),
        "slow_tests": []
    })
    print(f"✅ Results written to {jcc_results_path}")
    
    print(f"\n🔚 Test execution completed.")

if __name__ == "__main__":
    print("🧪 Jarvis Command Parsing Test Suite")
    print("=" * 50)
    print("Usage examples:")
    print("  python3 test_command_parsing.py                    # Run all tests")
    print("  python3 test_command_parsing.py -l                 # List all tests with indices")
    print("  python3 test_command_parsing.py -t 5               # Run only test #5")
    print("  python3 test_command_parsing.py -t 5 7 11         # Run tests #5, #7, and #11")
    print("  python3 test_command_parsing.py -c calculate      # Run only calculator tests")
    print("  python3 test_command_parsing.py -o results.json   # Write results to custom file")
    print("  python3 test_command_parsing.py -c get_sports -o sports_results.json")
    print("=" * 50)
    print()
    
    main()
