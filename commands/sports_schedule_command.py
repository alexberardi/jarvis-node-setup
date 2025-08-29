import json
import time
from typing import List, Any, Optional

from pydantic import BaseModel
from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from core.request_information import RequestInformation
from jarvis_services.espn_sports_service import ESPNSportsService, League
from clients.jarvis_command_center_client import JarvisCommandCenterClient


class SportsScheduleCommand(IJarvisCommand):
    """Command for getting sports schedules and upcoming games"""
    
    def __init__(self):
        pass
    
    @property
    def command_name(self) -> str:
        return "sports_schedule_command"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "schedule", "when", "next game", "play", "upcoming", "game time", "what time",
            "nfl", "nba", "mlb", "nhl", "college football", "college basketball"
        ]
    
    @property
    def description(self) -> str:
        return "Get sports schedules, find when teams play next, check upcoming games, and get game times for NFL, NBA, MLB, NHL, and college sports."
    
    def generate_examples(self, date_context: DateContext) -> str:
        """Generate examples for the sports schedule command with varied verbiage"""
        return f"""
        Voice Command: "When do the Giants play next?"
        → Output:
        {{{{"s":true,"n":"sports_schedule_command","p":{{{{"nickname":"Giants","datetimes":["{date_context.current.utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "What's the New York Giants schedule for this weekend?"
        → Output:
        {{{{"s":true,"n":"sports_schedule_command","p":{{{{"nickname":"Giants","location":"New York","datetimes":["{date_context.weekend.this_weekend[0].utc_start_of_day}","{date_context.weekend.this_weekend[1].utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "Show me the Panthers upcoming games for next week"
        → Output:
        {{{{"s":true,"n":"sports_schedule_command","p":{{{{"nickname":"Panthers","datetimes":["{date_context.weeks.next_week[0].utc_start_of_day}","{date_context.weeks.next_week[1].utc_start_of_day}","{date_context.weeks.next_week[2].utc_start_of_day}","{date_context.weeks.next_week[3].utc_start_of_day}","{date_context.weeks.next_week[4].utc_start_of_day}","{date_context.weeks.next_week[5].utc_start_of_day}","{date_context.weeks.next_week[6].utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "What time is the Carolina Panthers game tomorrow?"
        → Output:
        {{{{"s":true,"n":"sports_schedule_command","p":{{{{"nickname":"Panthers","location":"Carolina","datetimes":["{date_context.relative_dates.tomorrow.utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "When's the next Giants game?"
        → Output:
        {{{{"s":true,"n":"sports_schedule_command","p":{{{{"nickname":"Giants","datetimes":["{date_context.current.utc_start_of_day}"]}}}},"e":null}}}}
        """
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("nickname", "string", required=True, description="Team name, nickname, or college name the user spoke. Examples: 'Giants', 'Yanks', 'Ohio State', 'Bama', 'Vols'. "),
            JarvisParameter("location", "string", required=False, description="Geographic identifier that appears BEFORE the team name in the voice command. Examples: 'New York' from 'New York Giants', 'Carolina' from 'Carolina Panthers', 'Golden State' from 'Golden State Warriors', 'New England' from 'New England Patriots'. Extract this when a geographic identifier precedes the team name to avoid ambiguity."),
            JarvisParameter("datetimes", "datetime", required=True, description="Array of ISO datetime strings to check for schedules. If no specific date is mentioned, use today's date: [date_context.current.utc_start_of_day]. For relative dates like 'this weekend' or 'next week', convert them to actual ISO datetime values using the DateContext.")
        ]
    
    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []
    
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        # Get parameters
        team_name = kwargs.get("nickname")
        location = kwargs.get("location")
        datetimes = kwargs.get("datetimes", [])
        
        # Extract voice command
        voice_command = request_info.voice_command
        
        print(f"🔍 Sports Schedule Command - Voice: '{voice_command}'")
        print(f"🔍 Parameters: team_name={team_name}, location={location}, datetimes={datetimes}")
        if datetimes:
            print(f"🔍 Raw datetimes: {datetimes}")
            print(f"🔍 Datetime type: {type(datetimes[0]) if datetimes else 'None'}")
        
        # Validate team name
        if not team_name:
            return CommandResponse.error_response(
                speak_message="I need to know which team you're asking about. Please specify a team name.",
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
                    speak_message=f"I'm sorry, but I couldn't find any teams matching '{team_name}'. Please check the team name and try again.",
                    error_details=f"No teams found for: {team_name}",
                    context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_found": 0
                    }
                )
            
            print(f"🔍 Found {len(teams)} team(s) for '{team_name}':")
            for team in teams:
                print(f"   • {team.full_name} ({team.league.value.upper()})")
            
            # If multiple teams and city provided, filter by city
            if len(teams) > 1 and location:
                print(f"🔍 Filtering by location: {location}")
                pro_teams = [t for t in teams if t.league in [League.NFL, League.NBA, League.MLB, League.NHL]]
                filtered_teams = [t for t in pro_teams if location.lower() in t.city.lower()]
                
                if filtered_teams:
                    teams = filtered_teams
                    print(f"✅ Filtered to {len(teams)} team(s) in {location}")
                else:
                    print(f"⚠️  No teams found in {location}, using all {len(teams)} teams")
            
            # Find the next upcoming event for the team
            next_event = self._find_next_event(teams, espn_service)
            if next_event:
                return self._craft_next_event_response(next_event, voice_command, team_name, teams)
            else:
                return CommandResponse.follow_up_response(
                    speak_message=f"I couldn't find any upcoming games for {team_name} in the next 10 days.",
                    context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                        "next_event_found": False
                    }
                )
                
        except Exception as e:
            print(f"❌ Error in sports schedule command: {e}")
            return CommandResponse.error_response(
                speak_message=f"I'm sorry, but I encountered an error while looking up the schedule for {team_name}. Please try again.",
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
            speak_message=f"Found upcoming game for {team_name}",
            context_data={
                "voice_command": voice_command,
                "team_name": team_name,
                "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                "next_event": next_event
            }
        )
