from typing import List, Any, Optional
from pydantic import BaseModel

from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from core.command_response import CommandResponse
from jarvis_services.espn_sports_service import ESPNSportsService, League
from clients.jarvis_command_center_client import JarvisCommandCenterClient
from utils.config_service import Config
from utils.date_util import extract_dates_from_datetimes
from utils.timezone_util import format_datetime_local


# NOTE: ESPN API dates are converted from UTC to user's local timezone in espn_sports_service.py


class SportsResponse(BaseModel):
    """Response model for LLM-generated sports responses"""
    response: str


class SportsScoreCommand(IJarvisCommand):
    """Command for getting sports scores and results"""
    
    def __init__(self):
        pass
    
    @property
    def command_name(self) -> str:
        return "sports_score_command"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "scores", "won", "lost", "win", "lose", "result", "game result", "how did", "what was the score"
        ]
    
    @property
    def description(self) -> str:
        return "Get sports scores and results for NFL, NBA, MLB, NHL, and college sports. "
    
    def generate_examples(self, date_context: DateContext) -> str:
        """Generate examples for the sports score command with varied verbiage"""
        return f"""
        Voice Command: "What's the score of the Giants game?"
        → Output:
        {{{{"s":true,"n":"sports_score_command","p":{{{{"nickname":"Giants","datetimes":["{date_context.current.utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "How did the Seattle Mariners do yesterday?"
        → Output:
        {{{{"s":true,"n":"sports_score_command","p":{{{{"nickname":"Mariners","location":"Seattle","datetimes":["{date_context.relative_dates.yesterday.utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "What was the Panthers score last weekend?"
        → Output:
        {{{{"s":true,"n":"sports_score_command","p":{{{{"nickname":"Panthers","datetimes":["{date_context.weekend.last_weekend[0].utc_start_of_day}","{date_context.weekend.last_weekend[1].utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "Show me the Baltimore Orioles game result from last weekend"
        → Output:
        {{{{"s":true,"n":"sports_score_command","p":{{{{"nickname":"Orioles","location":"Baltimore","datetimes":["{date_context.weekend.last_weekend[0].utc_start_of_day}","{date_context.weekend.last_weekend[1].utc_start_of_day}"]}}}},"e":null}}}}

        Voice Command: "What's the Minnesota Twins score?"
        → Output:
        {{{{"s":true,"n":"sports_score_command","p":{{{{"nickname":"Twins", "location": "Minnesota", "datetimes":["{date_context.current.utc_start_of_day}"]}}}},"e":null}}}}
        """
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("nickname", "string", required=True, description="Team name, nickname, or college name the user spoke. Examples: 'Giants', 'Yanks', 'Ohio State', 'Bama', 'Vols'. "),
            JarvisParameter("location", "string", required=False, description="[CONDITIONALLY REQUIRED]: If the voice command contains a geographic identifier before the team nickname (e.g., “Chicago Bulls”, “New York Giants”, “Golden State Warriors”), you must include it in location verbatim as spoken, even if the nickname alone is unambiguous. Do not drop it."),
            JarvisParameter("datetimes", "array[datetime]", required=False, description="Array of ISO datetime strings to check for scores. ")
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
        
        print(f"🔍 Sports Score Command - Voice: '{voice_command}'")
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
            
            # Handle different actions
            # Get scores for the resolved teams (existing logic for get_scores action)
            all_games = []
            # Extract dates from datetimes if provided, otherwise default to today
            dates_to_check = []
            if datetimes and len(datetimes) > 0:
                try:
                    dates_to_check = extract_dates_from_datetimes(datetimes)
                    print(f"🔍 Extracted dates to check: {dates_to_check}")
                except Exception as e:
                    print(f"⚠️  Error extracting dates from datetimes '{datetimes}': {e}")
                    print(f"⚠️  Falling back to today's date")
                    dates_to_check = []
            
            # If no dates specified, default to today
            if not dates_to_check:
                from datetime import datetime
                dates_to_check = [datetime.now().strftime('%Y-%m-%d')]
                print(f"🔍 No dates specified, defaulting to today: {dates_to_check}")
            
            # Loop through each date and get scores for all teams
            for date in dates_to_check:
                # Convert YYYY-MM-DD format to YYYYMMDD format for ESPN API
                espn_date = date.replace('-', '')
                print(f"🔍 Checking scores for date: {date} (ESPN format: {espn_date})")
                for team in teams:
                    try:
                        team_games = espn_service.get_team_scores(team_name, espn_date)
                        if team_games:
                            print(f"✅ Found {len(team_games)} games for {team.full_name} on {date}")
                            all_games.extend(team_games)
                        else:
                            print(f"ℹ️  No games found for {team.full_name} on {date}")
                    except Exception as e:
                        print(f"❌ Error getting scores for {team.league.value} on {date}: {e}")
                        continue
            
            # If no games found
            if not all_games:
                if len(dates_to_check) == 1:
                    date_display = dates_to_check[0]
                else:
                    date_display = f"{len(dates_to_check)} dates ({', '.join(dates_to_check)})"
                
                return CommandResponse.follow_up_response(
                    speak_message=f"I checked, but there are no games for {team_name} on {date_display}.",
                    context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                        "games_found": 0,
                        "dates_checked": dates_to_check
                    }
                )
            
            # Use LLM to craft natural response
            try:
                jcc_client = JarvisCommandCenterClient(Config.get("jarvis_command_center_api_url"))
                
                # Prepare data for LLM - group games by opponent for clarity
                games_by_opponent = {}
                for game in all_games:
                    # Determine the opponent (the team that's NOT the requested team)
                    if team_name.lower() in game.home_team.lower():
                        opponent = game.away_team
                        is_home = True
                    else:
                        opponent = game.home_team
                        is_home = False
                    
                    if opponent not in games_by_opponent:
                        games_by_opponent[opponent] = []
                    
                    game_info = {
                        "opponent": opponent,
                        "is_home": is_home,
                        "home_team": game.home_team,
                        "away_team": game.away_team,
                        "home_score": game.home_score,
                        "away_score": game.away_score,
                        "status": game.status,
                        "league": game.league.value.upper(),
                        "venue": game.venue,
                        "broadcast": game.broadcast,
                        "date": game.start_time.strftime('%Y-%m-%d') if game.start_time else None
                    }
                    if game.start_time:
                        game_info["start_time"] = format_datetime_local(game.start_time, "%I:%M %p")
                    games_by_opponent[opponent].append(game_info)
                
                # Sort games by date within each opponent group
                for opponent in games_by_opponent:
                    games_by_opponent[opponent].sort(key=lambda x: x["date"] if x["date"] else "9999-99-99")
                
                # Create prompt for LLM
                prompt = f"""You are a sports information assistant.

USER REQUEST: {voice_command}

TEAM: {team_name}
CITY: {location if location else 'Not specified'}

SPORTS DATA:
- Teams: {[t.full_name for t in teams]}
- Dates: {', '.join(dates_to_check) if dates_to_check else 'today'}
- Games found: {len(all_games)}

GAMES BY OPPONENT:
{chr(10).join([f"vs {opponent} ({len(games)} game(s)):" + chr(10) + chr(10).join([f"  • {g['date']} - {g['away_team']} @ {g['home_team']} - STATUS: {g['status'].upper()}" + (f" - Final Score: {g['away_team']} {g['away_score']}, {g['home_team']} {g['home_score']}" if g['home_score'] is not None and g['away_score'] is not None and g['status'] == 'final' else f" - Current Score: {g['away_team']} {g['away_score']}, {g['home_team']} {g['home_score']}" if g['home_score'] is not None and g['away_score'] is not None else "") for g in games]) for opponent, games in games_by_opponent.items()])}

RESPONSE REQUIREMENTS:
- Be conversational and natural for voice
- Use sportscaster language
- Answer the question first, then provide details
- Include all games found
- Use correct status terminology (Final Score, Current Score, Scheduled)
- Mention leagues if multiple sports found

RESPONSE FORMAT:
You must return EXACTLY this JSON and nothing else:

{{"response": "your message here"}}

EXAMPLES OF WHAT NOT TO RETURN:
❌ "Hey there, baseball fan! {{"response": "message"}}"
❌ "{{"response": "message"}} I'll keep an eye on it for you."
❌ "Here's the info: {{"response": "message"}}"
❌ Any text before or after the JSON

ONLY RETURN THIS EXACT FORMAT:
{{"response": "your message here"}}"""
                
                # Get LLM response
                # llm_response = jcc_client.chat(prompt, SportsResponse)
                # print(f"LLM response: {llm_response}")
                
                # if llm_response and hasattr(llm_response, 'response'):
                #     message = llm_response.response.strip()
                #     print(f"LLM crafted response: {message}")
                # else:
                #     # Fallback response
                #     message = f"I found {len(all_games)} game(s) for {team_name}. "
                #     if len(teams) > 1:
                #         message += f"This includes teams from {', '.join([t.league.value.upper() for t in teams])}. "
                    
                #     for game in all_games:
                #         if game.status == "final" and game.home_score is not None and game.away_score is not None:
                #             message += f"{game.away_team} {game.away_score} at {game.home_team} {game.home_score} ({game.league.value.upper()}). "
                #         elif game.start_time:
                #             message += f"{game.away_team} at {game.home_team} at {format_datetime_local(game.start_time, '%I:%M %p')} ({game.league.value.upper()}). "
                #         else:
                #             message += f"{game.away_team} at {game.home_team} ({game.league.value.upper()}). "
                
                return CommandResponse.follow_up_response(
                    speak_message='', #message,
                    context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                        "games_found": len(all_games),
                        "games_data": [game for games in games_by_opponent.values() for game in games],
                        "dates_checked": dates_to_check
                    }
                )
                
            except Exception as e:
                print(f"❌ Error getting LLM response: {e}")
                # Fallback to simple response
                message = f"I found {len(all_games)} game(s) for {team_name}."
                return CommandResponse.follow_up_response(
                    speak_message=message,
                    context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                        "games_found": len(all_games),
                        "error": str(e)
                    }
                )
                
        except Exception as e:
            return CommandResponse.error_response(
                speak_message=f"I'm sorry, but I encountered an error while trying to get sports information: {str(e)}",
                error_details=str(e),
                context_data={
                    "voice_command": voice_command,
                    "team_name": team_name,
                    "error": str(e)
                }
            )

    def _find_next_event(self, teams, espn_service):
        """Find the next upcoming event for the given teams by checking the next 10 days"""
        from datetime import datetime, timedelta
        
        print(f"🔍 Searching for next event for {len(teams)} team(s) over the next 10 days...")
        
        # Check the next 10 days
        today = datetime.now()
        for day_offset in range(1, 11):  # 1 to 10 days from now
            check_date = today + timedelta(days=day_offset)
            date_str = check_date.strftime('%Y%m%d')
            print(f"🔍 Checking {check_date.strftime('%Y-%m-%d')} (ESPN format: {date_str})")
            
            for team in teams:
                try:
                    # Get games for this team on this date
                    games = espn_service.get_team_scores(team.nickname, date_str)
                    if games:
                        # Find the earliest game (first one in the list)
                        next_game = games[0]
                        print(f"✅ Found next event: {next_game.away_team} @ {next_game.home_team} on {check_date.strftime('%Y-%m-%d')}")
                        return {
                            'game': next_game,
                            'team': team,
                            'date': check_date,
                            'days_from_now': day_offset
                        }
                except Exception as e:
                    print(f"❌ Error checking {team.league.value} on {date_str}: {e}")
                    continue
        
        print(f"❌ No upcoming events found in the next 10 days")
        return None
    
    def _craft_next_event_response(self, next_event, voice_command, team_name, teams):
        """Craft a response for the next event using the LLM"""
        try:
            jcc_client = JarvisCommandCenterClient(Config.get("jarvis_command_center_api_url"))
            
            game = next_event['game']
            team = next_event['team']
            date = next_event['date']
            days_from_now = next_event['days_from_now']
            
            # Create prompt for LLM
            prompt = f"""
You are Jarvis, a voice assistant. Craft a natural, conversational spoken response for the user's request about the next game.

User's Request: "{voice_command}"

Team Information:
- Team Name: {team_name}
- Teams Resolved: {[t.full_name for t in teams]}
- Next Event Found: {days_from_now} day(s) from now

Next Game Details:
- Date: {date.strftime('%A, %B %d')}
- Matchup: {game.away_team} @ {game.home_team}
- League: {game.league.value.upper()}
- Status: {game.status.upper()}
- Venue: {game.venue if game.venue else 'TBD'}
- Broadcast: {game.broadcast if game.broadcast else 'TBD'}
- Start Time: {game.start_time.strftime('%I:%M %p') if game.start_time else 'TBD'}

IMPORTANT GUIDELINES:
- This is a VOICE assistant - never say "let me show you", "see the details", or other visual references
- Use sportscaster-style language and terminology - be enthusiastic and engaging like a sports announcer
- CRITICAL: ALWAYS answer the user's question FIRST, then provide supporting details
- If the user asks "when is the next game?", start with the date/time: "The next game is on [date] at [time]!"
- If the user asks "who are they playing next?", start with the opponent: "They're taking on the [opponent] next!"
- Be conversational and natural, not robotic
- Speak as if you're talking directly to the user

CRITICAL JSON FORMATTING REQUIREMENTS:
- You MUST return ONLY a valid JSON object
- The JSON must have exactly this structure: {{"response": "your message here"}}
- NO text before the JSON
- NO text after the JSON
- NO explanations, notes, or additional content
- The response field must contain your spoken response

EXAMPLES OF CORRECT FORMAT:
✅ CORRECT: {{"response": "The next Giants game is on Friday, August 22nd at 7:00 PM! They'll be taking on the Eagles at Lincoln Financial Field in Philadelphia."}}

✅ CORRECT: {{"response": "They're playing the Cowboys next! The game is scheduled for this Saturday at 8:00 PM on NBC."}}

❌ INCORRECT: The next Giants game is on Friday at 7:00 PM
❌ INCORRECT: {{"response": "Game info here"}} - additional text
❌ INCORRECT: Here's the info: {{"response": "Game info here"}}

FINAL INSTRUCTION: Return ONLY the JSON object. Do NOT add any explanation, notes, or text before or after the JSON.

Return this exact JSON format and nothing else:
{{"response": "your message here"}}
"""
            
            # Get LLM response
            llm_response = jcc_client.chat(prompt, SportsResponse)
            
            if llm_response and hasattr(llm_response, 'response'):
                message = llm_response.response.strip()
                print(f"LLM crafted next event response: {message}")
            else:
                # Fallback response
                date_display = "tomorrow" if days_from_now == 1 else f"in {days_from_now} days"
                message = f"The next {team_name} game is on {date.strftime('%A, %B %d')} at {format_datetime_local(game.start_time, '%I:%M %p')}! {game.away_team} @ {game.home_team} ({game.league.value.upper()})."
            
            return CommandResponse.follow_up_response(
                speak_message="",#message,
                context_data={
                    "voice_command": voice_command,
                    "team_name": team_name,
                    "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                    "next_event": {
                        "game": {
                            "away_team": game.away_team,
                            "home_team": game.home_team,
                            "league": game.league.value,
                            "status": game.status,
                            "venue": game.venue,
                            "broadcast": game.broadcast,
                            "start_time": game.start_time.strftime('%I:%M %p') if game.start_time else None
                        },
                        "date": date.strftime('%Y-%m-%d'),
                        "days_from_now": days_from_now
                    }
                }
            )
            
        except Exception as e:
            print(f"❌ Error crafting next event response: {e}")
            # Fallback response
            date_display = "tomorrow" if next_event['days_from_now'] == 1 else f"in {next_event['days_from_now']} days"
            message = f"The next {team_name} game is {date_display} on {next_event['date'].strftime('%A, %B %d')}."
            
            return CommandResponse.follow_up_response(
                speak_message=message,
                context_data={
                    "voice_command": voice_command,
                    "team_name": team_name,
                    "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                    "error": str(e)
                }
            )
