from typing import List

from jarvis_log_client import JarvisLogger
from constants.relative_date_keys import RelativeDateKeys
from core.ijarvis_authentication import AuthenticationConfig
from core.ijarvis_command import IJarvisCommand, CommandExample
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from services.secret_service import get_secret_value
from jarvis_services.icloud_calendar_service import ICloudCalendarService
from jarvis_services.google_calendar_service import GoogleCalendarService
from utils.date_util import parse_date_array, format_date_display, dates_to_strings

logger = JarvisLogger(service="jarvis-node")

# Default OAuth client ID — same Google Cloud project as Gmail.
# Users can override via GOOGLE_CLIENT_ID secret if they prefer their own.
_DEFAULT_CLIENT_ID = "683175564329-24fi9h6hck48hfrbjhb24vf12680e5ec.apps.googleusercontent.com"


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
            JarvisParameter("resolved_datetimes", "array<datetime>", description="Date keys like 'today', 'tomorrow', 'yesterday', 'this_weekend', 'next_week', etc. The server resolves these to actual dates.", required=True)
        ]

    def _get_calendar_type(self) -> str:
        """Read CALENDAR_TYPE from DB, defaulting to 'icloud'."""
        try:
            value = get_secret_value("CALENDAR_TYPE", "integration")
            return (value or "icloud").lower()
        except Exception:
            return "icloud"

    def _get_client_id(self) -> str:
        return get_secret_value("GOOGLE_CLIENT_ID", "integration") or _DEFAULT_CLIENT_ID

    @property
    def associated_service(self) -> str:
        return "Calendar"

    @property
    def setup_guide(self) -> str | None:
        cal_type = self._get_calendar_type()
        if cal_type == "google":
            return (
                "## Google Calendar\n\n"
                "1. Set **Calendar Type** to `google`\n"
                "2. Tap **Authenticate with Google Calendar** below\n"
                "3. Sign in with your Google account and grant calendar access\n\n"
                "That's it — tokens are managed automatically.\n\n"
                "> **Advanced**: A default OAuth client ID is provided. "
                "To use your own, set the **Client ID** field before authenticating.\n"
            )
        return (
            "## Apple iCloud Calendar\n\n"
            "Jarvis connects to your iCloud calendar using an **app-specific password** "
            "(not your main Apple ID password).\n\n"
            "### Generate an App-Specific Password\n\n"
            "1. Go to [appleid.apple.com](https://appleid.apple.com) and sign in\n"
            "2. In the **Sign-In and Security** section, click **App-Specific Passwords**\n"
            "3. Click **+** to generate a new password\n"
            "4. Name it something like `Jarvis Calendar`\n"
            "5. Copy the generated password (format: `xxxx-xxxx-xxxx-xxxx`)\n\n"
            "### Configure Jarvis\n\n"
            "- **Username**: Your Apple ID email (e.g., `you@icloud.com`)\n"
            "- **Password**: The app-specific password from step 5\n"
            "- **Default Calendar**: The exact name of your calendar (e.g., `Home`, `Work`). "
            "Leave blank to use all calendars.\n\n"
            "> **Note**: If you have two-factor authentication enabled (most accounts do), "
            "you **must** use an app-specific password. Your regular password will not work.\n"
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        cal_type = self._get_calendar_type()
        secrets: list[IJarvisSecret] = [
            JarvisSecret("CALENDAR_TYPE", "Type of calendar service (icloud, google)", "integration", "string", is_sensitive=False, friendly_name="Calendar Type"),
            JarvisSecret("CALENDAR_DEFAULT_NAME", "Default calendar name to use", "integration", "string", is_sensitive=False, friendly_name="Default Calendar"),
        ]
        if cal_type == "google":
            secrets.append(
                JarvisSecret("GOOGLE_CLIENT_ID", "Google OAuth client ID (optional — a default is provided)", "integration", "string", required=False, is_sensitive=False, friendly_name="Client ID (optional)"),
            )
        else:
            secrets.extend([
                JarvisSecret("CALENDAR_USERNAME", "Username/Apple ID for calendar service", "integration", "string", friendly_name="Username"),
                JarvisSecret("CALENDAR_PASSWORD", "Password/app-specific password for calendar service", "integration", "string", friendly_name="Password"),
            ])
        return secrets

    @property
    def all_possible_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret("CALENDAR_TYPE", "Type of calendar service (icloud, google)", "integration", "string", is_sensitive=False, friendly_name="Calendar Type"),
            JarvisSecret("CALENDAR_DEFAULT_NAME", "Default calendar name to use", "integration", "string", is_sensitive=False, friendly_name="Default Calendar"),
            JarvisSecret("CALENDAR_USERNAME", "Username/Apple ID for calendar service", "integration", "string", friendly_name="Username"),
            JarvisSecret("CALENDAR_PASSWORD", "Password/app-specific password for calendar service", "integration", "string", friendly_name="Password"),
            JarvisSecret("GOOGLE_CLIENT_ID", "Google OAuth client ID (optional — a default is provided)", "integration", "string", required=False, is_sensitive=False, friendly_name="Client ID (optional)"),
        ]

    @property
    def authentication(self) -> AuthenticationConfig | None:
        if self._get_calendar_type() != "google":
            return None
        client_id = self._get_client_id()
        return AuthenticationConfig(
            type="oauth",
            provider="google_calendar",
            friendly_name="Google Calendar",
            client_id=client_id,
            keys=["access_token", "refresh_token"],
            authorize_url="https://accounts.google.com/o/oauth2/v2/auth",
            exchange_url="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/calendar.readonly"],
            supports_pkce=True,
            extra_authorize_params={"access_type": "offline", "prompt": "consent"},
            requires_background_refresh=True,
            refresh_token_secret_key="GOOGLE_REFRESH_TOKEN",
        )

    def store_auth_values(self, values: dict[str, str]) -> None:
        """Store Google OAuth tokens from the mobile OAuth callback."""
        from services.secret_service import set_secret
        from services.command_auth_service import clear_auth_flag

        if "access_token" in values:
            set_secret("GOOGLE_ACCESS_TOKEN", values["access_token"], "integration")
        if "refresh_token" in values:
            set_secret("GOOGLE_REFRESH_TOKEN", values["refresh_token"], "integration")
        clear_auth_flag("google_calendar")

    @property
    def critical_rules(self) -> List[str]:
        return [
            "'day after tomorrow' = single key 'day_after_tomorrow', NOT two separate dates.",
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
        calendar_type = self._get_calendar_type()
        default_calendar = get_secret_value("CALENDAR_DEFAULT_NAME", "integration")

        try:
            # Initialize appropriate calendar service
            if calendar_type == "google":
                access_token = get_secret_value("GOOGLE_ACCESS_TOKEN", "integration")
                refresh_token = get_secret_value("GOOGLE_REFRESH_TOKEN", "integration")
                client_id = self._get_client_id()
                if not access_token:
                    return CommandResponse.error_response(
                        error_details="Google Calendar not authenticated. Complete OAuth setup first.",
                        context_data={"dates": dates_to_strings(target_dates), "events": [], "error": "Not authenticated"},
                    )
                calendar_service = GoogleCalendarService(
                    access_token=access_token,
                    refresh_token=refresh_token or "",
                    client_id=client_id or "",
                    calendar_id=default_calendar or "primary",
                )
            elif calendar_type == "icloud":
                username = get_secret_value("CALENDAR_USERNAME", "integration")
                password = get_secret_value("CALENDAR_PASSWORD", "integration")
                if not all([username, password]):
                    return CommandResponse.error_response(
                        error_details="Missing iCloud calendar credentials",
                        context_data={"dates": dates_to_strings(target_dates), "events": [], "error": "Missing credentials"},
                    )
                calendar_service = ICloudCalendarService(username, password, default_calendar or "default")
            else:
                return CommandResponse.error_response(
                    error_details=f"Unsupported calendar type: {calendar_type}",
                    context_data={"dates": dates_to_strings(target_dates), "events": [], "error": f"Unsupported calendar type: {calendar_type}"},
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
            
            # Check if iCloud service actually authenticated successfully
            if hasattr(calendar_service, '_authenticated') and not calendar_service._authenticated:
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
