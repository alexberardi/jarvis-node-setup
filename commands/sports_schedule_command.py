from typing import List

from constants.relative_date_keys import RelativeDateKeys
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
            "when do they play", "matchup"
        ]
    
    @property
    def description(self) -> str:
        return "Retrieve upcoming sports games and schedules for Big 4 (NFL, NBA, MLB, NHL) or College teams ONLY. Use for future games and matchups."
    
    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for the sports schedule command with varied verbiage"""
        return [
            CommandExample(
                voice_command="When do the Giants play next?",
                expected_parameters={"team_name": "Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]},
                is_primary=True
            ),
            CommandExample(
                voice_command="What's the New York Giants schedule for this weekend?",
                expected_parameters={"team_name": "New York Giants", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]}
            ),
            CommandExample(
                voice_command="Show me the Panthers upcoming games for next week",
                expected_parameters={"team_name": "Panthers", "resolved_datetimes": [RelativeDateKeys.NEXT_WEEK]}
            ),
            CommandExample(
                voice_command="What time is the Carolina Panthers game tomorrow?",
                expected_parameters={"team_name": "Carolina Panthers", "resolved_datetimes": [RelativeDateKeys.TOMORROW]}
            ),
            CommandExample(
                voice_command="When's the next Giants game?",
                expected_parameters={"team_name": "Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Consolidated for 3B model:
        - 1-2 examples per pattern variation
        - Focus on FUTURE-oriented keywords: "next", "when", "upcoming"
        - Distinguish from get_sports_scores (future vs past)
        """
        today = [RelativeDateKeys.TODAY]
        tomorrow = [RelativeDateKeys.TOMORROW]
        weekend_dates = [RelativeDateKeys.THIS_WEEKEND]
        next_week_dates = [RelativeDateKeys.NEXT_WEEK]

        examples: List[CommandExample] = [
            # === "When do [TEAM] play next?" ===
            CommandExample(voice_command="When do the Giants play next?", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}, is_primary=True),

            # === "When's the next [TEAM] game?" ===
            CommandExample(voice_command="When's the next Mets game?", expected_parameters={"team_name": "Mets", "resolved_datetimes": today}, is_primary=False),

            # === "Who do [TEAM] play next?" ===
            CommandExample(voice_command="Who do the Giants play next?", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}, is_primary=False),

            # === With date (base adapter handles resolution) ===
            CommandExample(voice_command="What time is the Giants game tomorrow?", expected_parameters={"team_name": "Giants", "resolved_datetimes": tomorrow}, is_primary=False),
            CommandExample(voice_command="What's the Giants schedule this weekend?", expected_parameters={"team_name": "Giants", "resolved_datetimes": weekend_dates}, is_primary=False),

            # === Full city + team name ===
            CommandExample(voice_command="When do the New York Giants play next?", expected_parameters={"team_name": "New York Giants", "resolved_datetimes": today}, is_primary=False),

            # === "Upcoming" keyword ===
            CommandExample(voice_command="Upcoming Giants games", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}, is_primary=False),
        ]
        return examples
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("team_name", "string", required=True, description="Team name as spoken; include city/school if said (e.g., 'Chicago Bulls', 'Ohio State')."),
            JarvisParameter("resolved_datetimes", "array<datetime>", required=True, description="ISO UTC start-of-day datetimes for the dates to check. Always required; use today's date if user doesn't specify.")
        ]
    
    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []
    
    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use this command for questions about FUTURE games, schedules, and upcoming events only",
            "Always include resolved_datetimes; if no date is specified, use today's start of day (current.utc_start_of_day from date context)",
            "Do NOT use this for ambiguous past results or results that could be far in the past",
            "This command is for 'when do they play next', 'what time is the game', 'who do they play', etc."
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="get_sports_scores",
                description="Past results, scores, 'how did [team] do', game outcomes, final scores."
            ),
            CommandAntipattern(
                command_name="search_web",
                description="Non-sports events like SpaceX launches, concerts, product releases, or eclipses."
            )
        ]
    
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        # Get parameters
        team_name = kwargs.get("team_name")
        datetimes = kwargs.get("resolved_datetimes")
        
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

        if not datetimes:
            return CommandResponse.error_response(
                error_details="Missing resolved_datetimes parameter",
                context_data={
                    "voice_command": voice_command,
                    "team_name": team_name,
                    "error": "Missing dates"
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
