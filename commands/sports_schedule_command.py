from typing import List

from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.command_response import CommandResponse
from core.request_information import RequestInformation
from jarvis_services.espn_sports_service import ESPNSportsService


class SportsScheduleCommand(IJarvisCommand):
    """Command for getting sports schedules and upcoming games"""
    
    def __init__(self):
        pass
    
    @property
    def command_name(self) -> str:
        return "get_sports_schedule"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "schedule", "when", "next game", "play next", "upcoming", "game time", "what time", 
            "when do they play", "when is the game", "next opponent", "who do they play next"
        ]
    
    @property
    def description(self) -> str:
        return "Upcoming games and schedules (future or later today). Returns opponents, times, venues, broadcast info. Not for past results or live updates."
    
    def generate_examples(self, date_context: DateContext) -> List[CommandExample]:
        """Generate examples for the sports schedule command with varied verbiage"""
        return [
            CommandExample(
                voice_command="When do the Giants play next?",
                expected_parameters={"team_name": "Giants", "resolved_datetimes": [date_context.current.utc_start_of_day]},
                is_primary=True
            ),
            CommandExample(
                voice_command="What's the New York Giants schedule for this weekend?",
                expected_parameters={"team_name": "New York Giants", "resolved_datetimes": [date_context.weekend.this_weekend[0].utc_start_of_day, date_context.weekend.this_weekend[1].utc_start_of_day]}
            ),
            CommandExample(
                voice_command="Show me the Panthers upcoming games for next week",
                expected_parameters={"team_name": "Panthers", "resolved_datetimes": [
                    date_context.weeks.next_week[0].utc_start_of_day,
                    date_context.weeks.next_week[1].utc_start_of_day,
                    date_context.weeks.next_week[2].utc_start_of_day,
                    date_context.weeks.next_week[3].utc_start_of_day,
                    date_context.weeks.next_week[4].utc_start_of_day,
                    date_context.weeks.next_week[5].utc_start_of_day,
                    date_context.weeks.next_week[6].utc_start_of_day
                ]}
            ),
            CommandExample(
                voice_command="What time is the Carolina Panthers game tomorrow?",
                expected_parameters={"team_name": "Carolina Panthers", "resolved_datetimes": [date_context.relative_dates.tomorrow.utc_start_of_day]}
            ),
            CommandExample(
                voice_command="When's the next Giants game?",
                expected_parameters={"team_name": "Giants", "resolved_datetimes": [date_context.current.utc_start_of_day]}
            )
        ]
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("team_name", "string", required=True, description="Team name as spoken; include city/school if said."),
            JarvisParameter("resolved_datetimes", "array", required=True, description="ISO UTC start-of-day datetimes for the dates to check.")
        ]
    
    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []
    
    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use this command for questions about FUTURE games, schedules, and upcoming events only",
            "Do NOT use this for ambiguous past results or results that could be far in the past",
            "This command is for 'when do they play next', 'what time is the game', 'who do they play', etc."
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="get_sports_scores",
                description="Past results, scores, or how a team did."
            )
        ]
    
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        # Get parameters
        team_name = kwargs.get("team_name")
        datetimes = kwargs.get("resolved_datetimes", [])
        
        # Extract voice command
        voice_command = request_info.voice_command
        
        
        # Validate team name
        if not team_name:
            return CommandResponse.error_response(
                                error_details="Missing team_name parameter",
                context_data={
                    "voice_command": voice_command,
                    "error": "Missing team name"
                }
            )
        
        try:
            # Initialize ESPN service
            espn_service = ESPNSportsService()
            
            # Resolve team name to actual teams
            teams = espn_service.resolve_team(team_name)
            
            if not teams:
                return CommandResponse.error_response(
                                        error_details=f"No teams found for: {team_name}",
                    context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_found": 0
                    }
                )
            
            
            
            # Find the next upcoming event for the team
            next_event = self._find_next_event(teams, espn_service)
            if next_event:
                return self._craft_next_event_response(next_event, voice_command, team_name, teams)
            else:
                return CommandResponse.follow_up_response(
                                        context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                        "next_event_found": False
                    }
                )
                
        except Exception as e:
            return CommandResponse.error_response(
                                error_details=str(e),
                context_data={
                    "voice_command": voice_command,
                    "team_name": team_name,
                    "error": str(e)
                }
            )
    
    def _find_next_event(self, teams, espn_service):
        """Find the next upcoming event for the given teams"""
        # This method will be implemented with the existing logic from sports_command
        # For now, returning None as placeholder
        return None
    
    def _craft_next_event_response(self, next_event, voice_command, team_name, teams):
        """Craft a response for the next event"""
        # This method will be implemented with the existing logic from sports_command
        # For now, returning a placeholder response
        return CommandResponse.success_response(
                        context_data={
                "voice_command": voice_command,
                "team_name": team_name,
                "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                "next_event": next_event
            }
        )
