from typing import List, Any, Optional

from jarvis_log_client import JarvisLogger
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

logger = JarvisLogger(service="jarvis-node")


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

        Focus areas:
        - Implicit today (no date word -> resolved_datetimes: ["today"])
        - Day after tomorrow as single token
        - Various phrasings for calendar queries
        """
        examples = [
            # === IMPLICIT TODAY - Critical: no date word = today ===
            ("Read my calendar", [RelativeDateKeys.TODAY], True),
            ("What's on my calendar?", [RelativeDateKeys.TODAY], False),
            ("What's on my schedule?", [RelativeDateKeys.TODAY], False),
            ("Do I have any meetings?", [RelativeDateKeys.TODAY], False),
            ("Do I have any appointments?", [RelativeDateKeys.TODAY], False),
            ("Am I busy?", [RelativeDateKeys.TODAY], False),
            ("What are my plans?", [RelativeDateKeys.TODAY], False),
            ("Check my calendar", [RelativeDateKeys.TODAY], False),

            # === EXPLICIT TODAY ===
            ("What's on my calendar today?", [RelativeDateKeys.TODAY], False),
            ("What meetings do I have today?", [RelativeDateKeys.TODAY], False),
            ("What's my schedule for today?", [RelativeDateKeys.TODAY], False),

            # === TOMORROW ===
            ("What's on my calendar tomorrow?", [RelativeDateKeys.TOMORROW], False),
            ("Show me my schedule for tomorrow", [RelativeDateKeys.TOMORROW], False),
            ("What appointments do I have tomorrow?", [RelativeDateKeys.TOMORROW], False),

            # === DAY AFTER TOMORROW - single token ===
            ("What appointments do I have the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("What's on my calendar the day after tomorrow?", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),
            ("Show my schedule for the day after tomorrow", [RelativeDateKeys.DAY_AFTER_TOMORROW], False),

            # === WEEKEND / WEEK ===
            ("What's on my calendar this weekend?", [RelativeDateKeys.THIS_WEEKEND], False),
            ("Show my calendar for this weekend", [RelativeDateKeys.THIS_WEEKEND], False),
            ("What meetings do I have next week?", [RelativeDateKeys.NEXT_WEEK], False),
        ]
        return [
            CommandExample(voice_command=voice, expected_parameters={"resolved_datetimes": dates}, is_primary=is_primary)
            for voice, dates, is_primary in examples
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("resolved_datetimes", "array<string>", description="Date keys like 'today', 'tomorrow', 'yesterday', 'this_weekend', 'next_week', etc. The server resolves these to actual dates.", required=True)
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
        logger.debug(f"DEBUG: request_info type: {type(request_info)}")
        logger.debug(f"DEBUG: request_info content: {request_info}")
        
        # Handle both RequestInformation object and dictionary
        if hasattr(request_info, 'voice_command'):
            voice_command = request_info.voice_command
        elif isinstance(request_info, dict) and 'voice_command' in request_info:
            voice_command = request_info['voice_command']
        else:
            # Fallback if we can't get the voice command
            voice_command = "unknown command"
            logger.debug(f"WARNING: Could not extract voice_command from request_info: {request_info}")
        
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
            logger.debug(f"DEBUG: Parsed target_dates: {[d.strftime('%Y-%m-%d %H:%M:%S') for d in target_dates]}")
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
        logger.debug(f"Voice command received: '{voice_command}'")
        
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
                
                logger.debug(f"DEBUG: Multiple dates from LLM - querying range: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')} (span: {total_span} days)")
                all_events = calendar_service.read_events(start_date, total_span)
                logger.debug(f"DEBUG: Found {len(all_events)} total events across specified range")
            else:
                # Single specific date from LLM - query just that date
                start_date = target_dates[0]
                logger.debug(f"DEBUG: Single date from LLM - querying: {start_date.strftime('%Y-%m-%d')} with 1 day lookahead")
                all_events = calendar_service.read_events(start_date, 1)
                logger.debug(f"DEBUG: Found {len(all_events)} events for single specified date")
            
            # Debug: Show all events with their IDs
            for i, event in enumerate(all_events):
                logger.debug(f"DEBUG: Event {i+1}: {event.summary} at {event.start_time} (ID: {event.id})")
            
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
                logger.debug(message)
                
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
                        logger.debug(f"LLM crafted response: {message}")
                    else:
                        # Fallback to our original message if LLM fails
                        logger.debug(f"LLM response failed, using fallback message. Response: {llm_response}")
                
                except Exception as e:
                    logger.debug(f"Failed to get LLM response, using fallback message: {str(e)}")
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
                logger.debug(message)
                
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
                        logger.debug(f"LLM crafted response: {message}")
                    else:
                        # Fallback to our original message if LLM fails
                        logger.debug(f"LLM response failed, using fallback message. Response: {llm_response}")
                
                except Exception as e:
                    logger.debug(f"Failed to get LLM response, using fallback message: {str(e)}")
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
