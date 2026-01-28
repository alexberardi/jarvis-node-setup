from typing import List, Any, Optional

from pydantic import BaseModel
from constants.relative_date_keys import RelativeDateKeys
from core.ijarvis_command import IJarvisCommand, CommandExample
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from services.secret_service import get_secret_value
from utils.config_service import Config
from jarvis_services.icloud_calendar_service import ICloudCalendarService
from utils.date_util import parse_date_array, format_date_display, dates_to_strings
from clients.jarvis_command_center_client import JarvisCommandCenterClient


class ReadCalendarCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "get_calendar_events"

    @property
    def keywords(self) -> List[str]:
        return ["calendar", "events", "schedule", "appointments", "meetings", "what's on", "today's events", "agenda", "plans"]

    @property
    def description(self) -> str:
        return "Retrieve calendar events for specified dates or date ranges. Use for ALL calendar and scheduling queries."

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise example utterances with expected parameters using date keys"""
        return [
            CommandExample(
                voice_command="What's on my calendar today?",
                expected_parameters={"resolved_datetimes": [RelativeDateKeys.TODAY]},
                is_primary=True
            ),
            CommandExample(
                voice_command="Show me my schedule for tomorrow",
                expected_parameters={"resolved_datetimes": [RelativeDateKeys.TOMORROW]}
            ),
            CommandExample(
                voice_command="What appointments do I have the day after tomorrow?",
                expected_parameters={"resolved_datetimes": [RelativeDateKeys.DAY_AFTER_TOMORROW]}
            ),
            CommandExample(
                voice_command="Show my calendar for this weekend",
                expected_parameters={"resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}
            ),
            CommandExample(
                voice_command="Read my calendar",
                expected_parameters={"resolved_datetimes": [RelativeDateKeys.TODAY]}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Optimized for 3B model:
        - Heavy repetition of "calendar/schedule" patterns
        - Always include resolved_datetimes (required param)
        - Clear "no date = today" reinforcement
        """
        examples = [
            # === CRITICAL: "What's on my calendar" patterns - no date = today ===
            ("What's on my calendar?", [RelativeDateKeys.TODAY], True),
            ("What do I have on my calendar?", [RelativeDateKeys.TODAY], False),
            ("Check my calendar", [RelativeDateKeys.TODAY], False),
            ("Show me my calendar", [RelativeDateKeys.TODAY], False),
            ("Read my calendar", [RelativeDateKeys.TODAY], False),
            ("My calendar", [RelativeDateKeys.TODAY], False),

            # === CRITICAL: "meetings/appointments/schedule" - no date = today ===
            ("Do I have any meetings?", [RelativeDateKeys.TODAY], False),
            ("Any meetings?", [RelativeDateKeys.TODAY], False),
            ("Any appointments?", [RelativeDateKeys.TODAY], False),
            ("What's my schedule?", [RelativeDateKeys.TODAY], False),
            ("Show my schedule", [RelativeDateKeys.TODAY], False),
            ("What's my day look like?", [RelativeDateKeys.TODAY], False),
            ("Am I busy?", [RelativeDateKeys.TODAY], False),

            # === Explicit "today" patterns ===
            ("What's on my calendar today?", [RelativeDateKeys.TODAY], False),
            ("Do I have any meetings today?", [RelativeDateKeys.TODAY], False),
            ("What meetings do I have today?", [RelativeDateKeys.TODAY], False),
            ("Check my calendar for today", [RelativeDateKeys.TODAY], False),
            ("Show me today's schedule", [RelativeDateKeys.TODAY], False),
            ("Any appointments today?", [RelativeDateKeys.TODAY], False),
            ("Am I busy today?", [RelativeDateKeys.TODAY], False),
            ("What's happening today?", [RelativeDateKeys.TODAY], False),

            # === "Tomorrow" patterns ===
            ("What's on my calendar tomorrow?", [RelativeDateKeys.TOMORROW], False),
            ("Show me my schedule for tomorrow", [RelativeDateKeys.TOMORROW], False),
            ("Any meetings tomorrow?", [RelativeDateKeys.TOMORROW], False),
            ("Any appointments tomorrow?", [RelativeDateKeys.TOMORROW], False),
            ("What do I have tomorrow?", [RelativeDateKeys.TOMORROW], False),
            ("Am I busy tomorrow?", [RelativeDateKeys.TOMORROW], False),
            ("Check my calendar for tomorrow", [RelativeDateKeys.TOMORROW], False),
            ("Tomorrow's schedule", [RelativeDateKeys.TOMORROW], False),
            ("Meetings tomorrow", [RelativeDateKeys.TOMORROW], False),

            # === Day after tomorrow (CRITICAL: must resolve to day_after_tomorrow, NOT a literal date or tomorrow+1) ===
            ("What's on my calendar the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Any appointments the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Calendar for the day after tomorrow", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("What appointments do I have the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Do I have any meetings the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Show my schedule for the day after tomorrow", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Am I busy the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Am I free the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Check my calendar the day after tomorrow", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("What do I have the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Day after tomorrow calendar", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Day after tomorrow schedule", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Meetings the day after tomorrow", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Events the day after tomorrow", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("What events do I have the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Schedule for day after tomorrow", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),

            # === Weekend patterns ===
            ("What's on my calendar this weekend?", [RelativeDateKeys.THIS_WEEKEND], False),
            ("Show my calendar for this weekend", [RelativeDateKeys.THIS_WEEKEND], False),
            ("Any appointments this weekend?", [RelativeDateKeys.THIS_WEEKEND], False),
            ("Am I free this weekend?", [RelativeDateKeys.THIS_WEEKEND], False),
            ("Weekend plans?", [RelativeDateKeys.THIS_WEEKEND], False),
            ("Do I have anything this weekend?", [RelativeDateKeys.THIS_WEEKEND], False),

            # === Next week patterns ===
            ("What's on my calendar next week?", [RelativeDateKeys.NEXT_WEEK], False),
            ("What meetings do I have next week?", [RelativeDateKeys.NEXT_WEEK], False),
            ("Show my calendar for next week", [RelativeDateKeys.NEXT_WEEK], False),
            ("Next week's schedule", [RelativeDateKeys.NEXT_WEEK], False),
            ("Am I busy next week?", [RelativeDateKeys.NEXT_WEEK], False),

            # === Specific day of week ===
            ("What's on my calendar Monday?", [RelativeDateKeys.NEXT_MONDAY], False),
            ("Any meetings on Monday?", [RelativeDateKeys.NEXT_MONDAY], False),
            ("What do I have Tuesday?", [RelativeDateKeys.NEXT_TUESDAY], False),
            ("Calendar for Wednesday", [RelativeDateKeys.NEXT_WEDNESDAY], False),
            ("Any appointments Thursday?", [RelativeDateKeys.NEXT_THURSDAY], False),
            ("What's my schedule Friday?", [RelativeDateKeys.NEXT_FRIDAY], False),
            ("Saturday schedule", [RelativeDateKeys.NEXT_SATURDAY], False),

            # === Time-of-day references ===
            ("Am I free tonight?", [RelativeDateKeys.TONIGHT], False),
            ("Any meetings this morning?", [RelativeDateKeys.MORNING], False),
            ("Tomorrow morning meetings", [RelativeDateKeys.TOMORROW_MORNING], False),
        ]
        return [
            CommandExample(voice_command=voice, expected_parameters={"resolved_datetimes": dates}, is_primary=is_primary)
            for voice, dates, is_primary in examples
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("resolved_datetimes", "array<datetime>", description="ISO UTC start-of-day datetimes for the dates to retrieve events from.", required=True)
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret("CALENDAR_TYPE", "Type of calendar service (icloud, google)", "integration", "string"),
            JarvisSecret("CALENDAR_USERNAME", "Username/Apple ID for calendar service", "integration", "string"),
            JarvisSecret("CALENDAR_PASSWORD", "Password/app-specific password for calendar service", "integration", "string"),
            JarvisSecret("CALENDAR_DEFAULT_NAME", "Default calendar name to use", "integration", "string"),
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Always call this tool to read calendar events; do NOT ask for a date first",
            "Always include resolved_datetimes; use today's date if the user asks for 'today'.",
            "'the day after tomorrow' is a SINGLE date token 'day_after_tomorrow' - do NOT split it into multiple dates like 'tomorrow' + 'day_after_tomorrow'",
            "ALWAYS use symbolic date tokens in resolved_datetimes: 'today', 'tomorrow', 'day_after_tomorrow', 'this_weekend', 'next_week', etc. NEVER output literal dates like '2024-01-29' or '2026-01-29'"
        ]

    def run(self, request_info, **kwargs) -> CommandResponse:
        # Get parameters
        datetimes_array = kwargs.get("resolved_datetimes")
        
        # Debug: Check what type request_info actually is
        print(f"DEBUG: request_info type: {type(request_info)}")
        print(f"DEBUG: request_info content: {request_info}")
        
        # Handle both RequestInformation object and dictionary
        if hasattr(request_info, 'voice_command'):
            voice_command = request_info.voice_command
        elif isinstance(request_info, dict) and 'voice_command' in request_info:
            voice_command = request_info['voice_command']
        else:
            # Fallback if we can't get the voice command
            voice_command = "unknown command"
            print(f"WARNING: Could not extract voice_command from request_info: {request_info}")
        
        if not datetimes_array:
            return CommandResponse.error_response(
                error_details="Missing required resolved_datetimes parameter",
                context_data={
                    "dates": [],
                    "events": [],
                    "error": "Missing dates"
                }
            )

        # Parse datetime parameters
        try:
            target_dates = parse_date_array(datetimes_array)
            print(f"DEBUG: Parsed target_dates: {[d.strftime('%Y-%m-%d %H:%M:%S') for d in target_dates]}")
        except ValueError as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={
                    "dates": datetimes_array if datetimes_array else [],
                    "events": [],
                    "error": "Invalid datetime format"
                }
            )
        
        # Log the original voice command for debugging
        print(f"Voice command received: '{voice_command}'")
        
        # Get calendar configuration
        calendar_type = get_secret_value("CALENDAR_TYPE", "integration")
        username = get_secret_value("CALENDAR_USERNAME", "integration")
        password = get_secret_value("CALENDAR_PASSWORD", "integration")
        default_calendar = get_secret_value("CALENDAR_DEFAULT_NAME", "integration")
        
        if not all([calendar_type, username, password]):
            return CommandResponse.error_response(
                                error_details="Missing calendar configuration",
                context_data={
                    "dates": dates_to_strings(target_dates),
                    "events": [],
                    "error": "Missing calendar configuration"
                }
            )
        
        try:
            # Initialize appropriate calendar service
            if calendar_type.lower() == "icloud":
                calendar_service = ICloudCalendarService(username, password, default_calendar or "default")
            elif calendar_type.lower() == "google":
                # TODO: Implement Google Calendar service
                return CommandResponse.error_response(
                                        error_details="Google Calendar service not implemented",
                    context_data={
                        "dates": dates_to_strings(target_dates),
                        "events": [],
                        "error": "Google Calendar service not implemented"
                    }
                )
            else:
                return CommandResponse.error_response(
                                        error_details=f"Unsupported calendar type: {calendar_type}",
                    context_data={
                        "dates": dates_to_strings(target_dates),
                        "events": [],
                        "error": f"Unsupported calendar type: {calendar_type}"
                    }
                )
            
            # Collect events based on whether we have specific dates or are using the default
            all_events = []
            
            # Check if we're using specific dates from the LLM
            if len(target_dates) > 1:
                # Multiple specific dates from LLM - query the entire range
                start_date = target_dates[0]
                end_date = target_dates[-1]
                # Add 1 day buffer to catch events that span midnight
                total_span = (end_date - start_date).days + 1
                
                print(f"DEBUG: Multiple dates from LLM - querying range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} (span: {total_span} days)")
                all_events = calendar_service.read_events(start_date, total_span)
                print(f"DEBUG: Found {len(all_events)} total events across specified range")
            else:
                # Single specific date from LLM - query just that date
                start_date = target_dates[0]
                print(f"DEBUG: Single date from LLM - querying: {start_date.strftime('%Y-%m-%d')} with 1 day lookahead")
                all_events = calendar_service.read_events(start_date, 1)
                print(f"DEBUG: Found {len(all_events)} events for single specified date")
            
            # Debug: Show all events with their IDs
            for i, event in enumerate(all_events):
                print(f"DEBUG: Event {i+1}: {event.summary} at {event.start_time} (ID: {event.id})")
            
            # Check if we actually authenticated successfully
            if not hasattr(calendar_service, '_authenticated') or not calendar_service._authenticated:
                return CommandResponse.error_response(
                                        error_details="Calendar service authentication failed",
                    context_data={
                        "dates": dates_to_strings(target_dates),
                        "events": [],
                        "error": "Authentication failed"
                    }
                )
            
            if all_events:
                # Format events for response
                formatted_events = []
                for event in all_events:
                    formatted_event = {
                        "id": event.id,
                        "summary": event.summary,
                        "start_time": event.start_time.strftime("%H:%M") if not event.is_all_day else "All day",
                        "end_time": event.end_time.strftime("%H:%M") if not event.is_all_day else "All day",
                        "location": event.location,
                        "description": event.description,
                        "is_all_day": event.is_all_day
                    }
                    formatted_events.append(formatted_event)
                
                # Create summary message
                date_display = format_date_display(target_dates)
                message = f"You have {len(all_events)} event(s) on {date_display}"
                print(message)
                
                # Now use the LLM to craft a natural response based on the calendar data
                try:
                    jcc_client = JarvisCommandCenterClient(Config.get("jarvis_command_center_api_url"))
                    
                    # Prepare the calendar data for the LLM
                    calendar_data = {
                        "user_request": voice_command,
                        "target_dates": date_display,
                        "events": formatted_events,
                        "event_summary": f"{len(all_events)} event(s) found"
                    }
                    
                    # Create a prompt for the LLM to craft a natural response
                    prompt = f"""
You are Jarvis, a voice assistant. Craft a natural, conversational spoken response for the user's calendar request.

User's Request: "{voice_command}"

Calendar Data:
- Target Dates: {date_display}
- Number of Events: {len(all_events)}
- Events with Full Details:
{chr(10).join([f"  • {event.summary}{' at ' + event.location if event.location else ''} on {event.start_time.strftime('%A, %B %d at %I:%M %p')}{' (all day)' if event.is_all_day else ''}" for event in all_events])}

IMPORTANT GUIDELINES:
- This is a VOICE assistant - never say "let me show you", "see the details", "here's what I found", or other visual references
- ALWAYS include complete event details in your spoken response: event name, time, and location (if available)
- Be conversational and natural, not robotic
- Speak as if you're talking directly to the user
- Use the exact dates and times provided above

EXAMPLES:
- Good: "Yes, you have a dentist appointment on Friday at 2:30 PM at Downtown Dental."
- Bad: "You have an event scheduled. Let me show you the details."

Return ONLY a JSON object with this exact format: {{"response": "your natural spoken response here"}}
"""
                    
                    # Get the LLM response
                    llm_response = jcc_client.lightweight_chat(prompt, CalendarResponse)
                    
                    # Use the LLM response as our message
                    if llm_response and hasattr(llm_response, 'response'):
                        message = llm_response.response.strip()
                        print(f"LLM crafted response: {message}")
                    else:
                        # Fallback to our original message if LLM fails
                        print(f"LLM response failed, using fallback message. Response: {llm_response}")
                
                except Exception as e:
                    print(f"Failed to get LLM response, using fallback message: {str(e)}")
                    # Continue with our original message
                
                return CommandResponse.follow_up_response(
                                        context_data={
                        "dates": dates_to_strings(target_dates),
                        "calendar_type": calendar_type,
                        "calendar_name": default_calendar or "default",
                        "events": formatted_events,
                        "total_events": len(all_events),
                        "voice_command": voice_command,
                        "target_dates": target_dates,  # Include the actual datetime objects for follow-up context
                        "date_display": date_display
                    }
                )
            else:
                # No events found
                date_display = format_date_display(target_dates)
                message = f"No events found on {date_display}"
                print(message)
                
                # Now use the LLM to craft a natural response for the no-events case
                try:
                    jcc_client = JarvisCommandCenterClient(Config.get("jarvis_command_center_api_url"))
                    
                    # Create a prompt for the LLM to craft a natural response
                    prompt = f"""
Using the calendar information below, can you craft a natural, conversational response for the user's request?

User's Request: "{voice_command}"

Calendar Data:
- Target Dates: {date_display}
- Number of Events: 0
- Events: No events found

Please provide a helpful, conversational response that directly addresses what the user asked for.
Since there are no events, you might want to suggest alternatives or confirm the date range.

Return ONLY a JSON object with this exact format: {{"response": "your natural response here"}}
"""
                    
                    # Get the LLM response
                    llm_response = jcc_client.chat(prompt, CalendarResponse)
                    
                    # Use the LLM response as our message
                    if llm_response and hasattr(llm_response, 'response'):
                        message = llm_response.response.strip()
                        print(f"LLM crafted response: {message}")
                    else:
                        # Fallback to our original message if LLM fails
                        print(f"LLM response failed, using fallback message. Response: {llm_response}")
                
                except Exception as e:
                    print(f"Failed to get LLM response, using fallback message: {str(e)}")
                    # Continue with our original message
                
                return CommandResponse.follow_up_response(
                                        context_data={
                        "dates": dates_to_strings(target_dates),
                        "calendar_type": calendar_type,
                        "calendar_name": default_calendar or "default",
                        "events": [],
                        "total_events": 0,
                        "voice_command": voice_command,
                        "target_dates": target_dates,  # Include the actual datetime objects for follow-up context
                        "date_display": date_display
                    }
                )
                
        except Exception as e:
            return CommandResponse.error_response(
                                error_details=str(e),
                context_data={
                    "dates": dates_to_strings(target_dates),
                    "events": [],
                    "error": str(e)
                }
            )


class CalendarResponse(BaseModel):
    """Simple response model for LLM-generated calendar responses"""
    response: str
