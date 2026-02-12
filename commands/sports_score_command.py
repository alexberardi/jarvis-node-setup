from typing import List
from pydantic import BaseModel

from constants.relative_date_keys import RelativeDateKeys
from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from core.command_response import CommandResponse
from jarvis_services.espn_sports_service import ESPNSportsService
from clients.jarvis_command_center_client import JarvisCommandCenterClient
from utils.config_service import Config
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
        return "get_sports_scores"
    
    @property
    def keywords(self) -> List[str]:
        return [
            "scores", "won", "lost", "win", "lose", "result", "game result", "how did",
            "final score", "outcome", "beat", "defeated"
        ]
    
    @property
    def description(self) -> str:
        return "Retrieve final scores and results for active or recently completed games. Use for past game results and outcomes."
    
    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise examples for the sports score command with varied verbiage"""
        return [
            CommandExample(
                voice_command="How did the Giants do?",
                expected_parameters={"team_name": "Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]},
                is_primary=True
            ),
            CommandExample(
                voice_command="How did the Cowboys do?",
                expected_parameters={"team_name": "Cowboys", "resolved_datetimes": [RelativeDateKeys.TODAY]}
            ),
            CommandExample(
                voice_command="How did the New York Giants do?",
                expected_parameters={"team_name": "New York Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]}
            ),
            CommandExample(
                voice_command="How did the Eagles do last weekend?",
                expected_parameters={"team_name": "Eagles", "resolved_datetimes": [RelativeDateKeys.LAST_WEEKEND]}
            ),
            CommandExample(
                voice_command="What's the score of the Lakers game today?",
                expected_parameters={"team_name": "Lakers", "resolved_datetimes": [RelativeDateKeys.TODAY]}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Focus areas:
        - Implicit today (no date word -> resolved_datetimes: ["today"])
        - Team name extraction with various patterns
        - Distinguish from get_sports_schedule (this is for PAST results)
        """
        today = [RelativeDateKeys.TODAY]
        yesterday = [RelativeDateKeys.YESTERDAY]
        last_weekend = [RelativeDateKeys.LAST_WEEKEND]
        last_night = [RelativeDateKeys.LAST_NIGHT]

        examples: List[CommandExample] = [
            # === IMPLICIT TODAY - "How did [TEAM] do?" (no date = today) ===
            CommandExample(voice_command="How did the Giants do?", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}, is_primary=True),
            CommandExample(voice_command="How did the Cowboys do?", expected_parameters={"team_name": "Cowboys", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="How did the Dodgers do?", expected_parameters={"team_name": "Dodgers", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="How did the Lakers do?", expected_parameters={"team_name": "Lakers", "resolved_datetimes": today}, is_primary=False),

            # === CONTRACTIONS - "How'd [TEAM] do?" ===
            CommandExample(voice_command="How'd the Eagles do?", expected_parameters={"team_name": "Eagles", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="How'd the Packers do?", expected_parameters={"team_name": "Packers", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="How'd the Bears do?", expected_parameters={"team_name": "Bears", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="How'd the Chiefs do?", expected_parameters={"team_name": "Chiefs", "resolved_datetimes": today}, is_primary=False),

            # === IMPLICIT TODAY - "What's the score of [TEAM] game?" ===
            CommandExample(voice_command="What's the score of the Yankees game?", expected_parameters={"team_name": "Yankees", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="What's the score of the Knicks game?", expected_parameters={"team_name": "Knicks", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="What's the score of the Carolina Panthers game?", expected_parameters={"team_name": "Carolina Panthers", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="What was the score of the Mets game?", expected_parameters={"team_name": "Mets", "resolved_datetimes": today}, is_primary=False),

            # === FULL CITY + TEAM NAME ===
            CommandExample(voice_command="How did the New York Giants do?", expected_parameters={"team_name": "New York Giants", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="How did the Green Bay Packers do?", expected_parameters={"team_name": "Green Bay Packers", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="How did the Los Angeles Lakers do?", expected_parameters={"team_name": "Los Angeles Lakers", "resolved_datetimes": today}, is_primary=False),

            # === "Did [TEAM] win?" ===
            CommandExample(voice_command="Did the Celtics win?", expected_parameters={"team_name": "Celtics", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="Did the Patriots win?", expected_parameters={"team_name": "Patriots", "resolved_datetimes": today}, is_primary=False),

            # === "LAST NIGHT" - Critical pattern for evening games ===
            CommandExample(voice_command="How'd the Packers do last night?", expected_parameters={"team_name": "Packers", "resolved_datetimes": last_night}, is_primary=False),
            CommandExample(voice_command="How did the Bruins do last night?", expected_parameters={"team_name": "Bruins", "resolved_datetimes": last_night}, is_primary=False),
            CommandExample(voice_command="How'd the Bulls do last night?", expected_parameters={"team_name": "Bulls", "resolved_datetimes": last_night}, is_primary=False),
            CommandExample(voice_command="How'd the Celtics do last night?", expected_parameters={"team_name": "Celtics", "resolved_datetimes": last_night}, is_primary=False),
            CommandExample(voice_command="What was the score last night?", expected_parameters={"team_name": "", "resolved_datetimes": last_night}, is_primary=False),

            # === YESTERDAY ===
            CommandExample(voice_command="How did the Giants do yesterday?", expected_parameters={"team_name": "Giants", "resolved_datetimes": yesterday}, is_primary=False),
            CommandExample(voice_command="How did the Phillies do yesterday?", expected_parameters={"team_name": "Phillies", "resolved_datetimes": yesterday}, is_primary=False),
            CommandExample(voice_command="How'd the Yankees do yesterday?", expected_parameters={"team_name": "Yankees", "resolved_datetimes": yesterday}, is_primary=False),

            # === LAST WEEKEND ===
            CommandExample(voice_command="How did the Ravens do last weekend?", expected_parameters={"team_name": "Ravens", "resolved_datetimes": last_weekend}, is_primary=False),

            # === CASUAL / ABBREVIATED ===
            CommandExample(voice_command="Lakers score?", expected_parameters={"team_name": "Lakers", "resolved_datetimes": today}, is_primary=False),
            CommandExample(voice_command="Giants score?", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}, is_primary=False),
        ]
        return examples
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("team_name", "string", required=True, description="Team name as spoken; include city/school if said (e.g., 'Lakers', 'Alabama'). Must be valid Big 4 or College team."),
            JarvisParameter("resolved_datetimes", "array<string>", required=True, description="Date keys like 'today', 'yesterday', 'last_weekend'. Always required; use 'today' if user doesn't specify a date.")
        ]
    
    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Always include resolved_datetimes; if no date is specified, use today's start of day (current.utc_start_of_day from date context)",
            "Always call this tool for sports results; do NOT answer from memory or ask for a date first",
            "Never infer historical season dates when no date is mentioned; use today only",
            "Use this command for questions about PAST performance, results, scores, or 'how did [team] do'",
            "If the user is asking about upcoming games, schedules, or future matchups, do not use this command",
            "If the user is asking about championship winners or season outcomes (e.g., 'who won the Super Bowl', 'who won the World Series'), use search_web instead"
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="get_sports_schedule",
                description="Upcoming games, schedules, future matchups, 'when do they play next'."
            ),
            CommandAntipattern(
                command_name="search_web",
                description="Championship winners, season outcomes, 'who won the Super Bowl/World Series/NBA Finals', general web searches, news, or non-sports queries."
            ),
        ]
    
    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        # Get parameters
        team_name = kwargs.get("team_name")
        resolved_datetimes = kwargs.get("resolved_datetimes")
        
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

        if not resolved_datetimes:
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
            
            
            
            all_games = []
            dates_to_check = resolved_datetimes

            # Loop through each date and get scores for all teams
            for date in dates_to_check:
                # Convert YYYY-MM-DD format to YYYYMMDD format for ESPN API
                espn_date = date.replace('-', '')
                for team in teams:
                    try:
                        team_games = espn_service.get_team_scores(team_name, espn_date)
                        if team_games:
                            all_games.extend(team_games)
                    except Exception as e:
                        continue
            
            # If no games found
            if not all_games:
                if len(dates_to_check) == 1:
                    date_display = dates_to_check[0]
                else:
                    date_display = f"{len(dates_to_check)} dates ({', '.join(dates_to_check)})"
                
                return CommandResponse.follow_up_response(
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
                
                # if llm_response and hasattr(llm_response, 'response'):
                #     message = llm_response.response.strip()
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
                # Fallback to simple response
                message = f"I found {len(all_games)} game(s) for {team_name}."
                return CommandResponse.follow_up_response(
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
        
        # Check the next 10 days
        today = datetime.now()
        for day_offset in range(1, 11):  # 1 to 10 days from now
            check_date = today + timedelta(days=day_offset)
            date_str = check_date.strftime('%Y%m%d')
            
            for team in teams:
                try:
                    # Get games for this team on this date
                    games = espn_service.get_team_scores(team.nickname, date_str)
                    if games:
                        # Find the earliest game (first one in the list)
                        next_game = games[0]
                        return {
                            'game': next_game,
                            'team': team,
                            'date': check_date,
                            'days_from_now': day_offset
                        }
                except Exception as e:
                    continue
        
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
            else:
                # Fallback response
                date_display = "tomorrow" if days_from_now == 1 else f"in {days_from_now} days"
                message = f"The next {team_name} game is on {date.strftime('%A, %B %d')} at {format_datetime_local(game.start_time, '%I:%M %p')}! {game.away_team} @ {game.home_team} ({game.league.value.upper()})."
            
            return CommandResponse.follow_up_response(
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
            # Fallback response
            date_display = "tomorrow" if next_event['days_from_now'] == 1 else f"in {next_event['days_from_now']} days"
            message = f"The next {team_name} game is {date_display} on {next_event['date'].strftime('%A, %B %d')}."
            
            return CommandResponse.follow_up_response(
                                context_data={
                    "voice_command": voice_command,
                    "team_name": team_name,
                    "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                    "error": str(e)
                }
            )
