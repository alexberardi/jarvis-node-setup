from datetime import datetime, timedelta, date as date_type
from typing import Any, List

from constants.relative_date_keys import RelativeDateKeys
from core.command_response import CommandResponse
from core.ijarvis_command import IJarvisCommand, CommandAntipattern, CommandExample
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret
from core.request_information import RequestInformation
from core.validation_result import ValidationResult
from jarvis_services.espn_sports_service import ESPNSportsService, Game
from utils.timezone_util import format_datetime_local


class SportsCommand(IJarvisCommand):
    """Unified command for sports scores, live games, and upcoming schedules."""

    def __init__(self):
        pass

    @property
    def command_name(self) -> str:
        return "get_sports"

    @property
    def keywords(self) -> List[str]:
        return [
            "score", "scores", "won", "lost", "win", "lose", "result", "beat",
            "schedule", "when", "next game", "play next", "upcoming", "game time",
            "what time", "play tonight", "game tonight", "playing",
        ]

    @property
    def description(self) -> str:
        return (
            "Sports scores, live games, and upcoming schedules for Big 4 (NFL, NBA, MLB, NHL) "
            "and College teams. Handles past results, in-progress games, and future schedules. "
            "Requires a specific team name."
        )

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "team_name", "string", required=True,
                description="Team name as spoken (e.g., 'Lakers', 'New York Giants', 'Ohio State').",
            ),
            JarvisParameter(
                "resolved_datetimes", "array<datetime>", required=True,
                description="Date keys: 'today','tomorrow','yesterday','this_weekend','last_weekend','last_night','next_week'. Default 'today'.",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Requires a specific team name. Not for championships, award shows, or general 'who won' questions without a team.",
            "No date mentioned → use 'today'.",
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="search_web",
                description="General web searches, news, or queries without a specific team name.",
            ),
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="How did the Giants do?",
                expected_parameters={"team_name": "Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]},
                is_primary=True,
            ),
            CommandExample(
                voice_command="What time is the Nets game tonight?",
                expected_parameters={"team_name": "Nets", "resolved_datetimes": [RelativeDateKeys.TONIGHT]},
            ),
            CommandExample(
                voice_command="When do the Giants play next?",
                expected_parameters={"team_name": "Giants", "resolved_datetimes": [RelativeDateKeys.TODAY]},
            ),
            CommandExample(
                voice_command="Did the Lakers win last night?",
                expected_parameters={"team_name": "Lakers", "resolved_datetimes": [RelativeDateKeys.LAST_NIGHT]},
            ),
            CommandExample(
                voice_command="What's the Cowboys schedule this weekend?",
                expected_parameters={"team_name": "Cowboys", "resolved_datetimes": [RelativeDateKeys.THIS_WEEKEND]},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        today = [RelativeDateKeys.TODAY]
        yesterday = [RelativeDateKeys.YESTERDAY]
        tomorrow = [RelativeDateKeys.TOMORROW]
        last_night = [RelativeDateKeys.LAST_NIGHT]
        last_weekend = [RelativeDateKeys.LAST_WEEKEND]
        tonight = [RelativeDateKeys.TONIGHT]
        this_weekend = [RelativeDateKeys.THIS_WEEKEND]
        next_week = [RelativeDateKeys.NEXT_WEEK]

        return [
            # Past results — implicit today
            CommandExample(voice_command="How did the Giants do?", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}, is_primary=True),
            CommandExample(voice_command="How did the Cowboys do?", expected_parameters={"team_name": "Cowboys", "resolved_datetimes": today}),
            CommandExample(voice_command="How'd the Eagles do?", expected_parameters={"team_name": "Eagles", "resolved_datetimes": today}),
            CommandExample(voice_command="Did the Celtics win?", expected_parameters={"team_name": "Celtics", "resolved_datetimes": today}),
            CommandExample(voice_command="Lakers score?", expected_parameters={"team_name": "Lakers", "resolved_datetimes": today}),

            # Past results — explicit date
            CommandExample(voice_command="How'd the Packers do last night?", expected_parameters={"team_name": "Packers", "resolved_datetimes": last_night}),
            CommandExample(voice_command="How did the Giants do yesterday?", expected_parameters={"team_name": "Giants", "resolved_datetimes": yesterday}),
            CommandExample(voice_command="How did the Ravens do last weekend?", expected_parameters={"team_name": "Ravens", "resolved_datetimes": last_weekend}),

            # Score queries
            CommandExample(voice_command="What's the score of the Yankees game?", expected_parameters={"team_name": "Yankees", "resolved_datetimes": today}),
            CommandExample(voice_command="What was the score of the Mets game?", expected_parameters={"team_name": "Mets", "resolved_datetimes": today}),
            CommandExample(voice_command="How did the New York Giants do?", expected_parameters={"team_name": "New York Giants", "resolved_datetimes": today}),

            # Tonight / today schedule
            CommandExample(voice_command="Do the Nets play tonight?", expected_parameters={"team_name": "Nets", "resolved_datetimes": tonight}),
            CommandExample(voice_command="What time is the Nets game tonight?", expected_parameters={"team_name": "Nets", "resolved_datetimes": tonight}),
            CommandExample(voice_command="Is there a Yankees game today?", expected_parameters={"team_name": "Yankees", "resolved_datetimes": today}),

            # Future schedule
            CommandExample(voice_command="When do the Giants play next?", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}),
            CommandExample(voice_command="When's the next Mets game?", expected_parameters={"team_name": "Mets", "resolved_datetimes": today}),
            CommandExample(voice_command="What time is the Giants game tomorrow?", expected_parameters={"team_name": "Giants", "resolved_datetimes": tomorrow}),
            CommandExample(voice_command="What's the Giants schedule this weekend?", expected_parameters={"team_name": "Giants", "resolved_datetimes": this_weekend}),
            CommandExample(voice_command="Upcoming Giants games", expected_parameters={"team_name": "Giants", "resolved_datetimes": today}),
            CommandExample(voice_command="When do the New York Giants play next?", expected_parameters={"team_name": "New York Giants", "resolved_datetimes": today}),
            CommandExample(voice_command="Show me the Panthers upcoming games for next week", expected_parameters={"team_name": "Panthers", "resolved_datetimes": next_week}),
        ]

    def validate_call(self, **kwargs: Any) -> list[ValidationResult]:
        results = super().validate_call(**kwargs)

        team_name: str = kwargs.get("team_name", "")
        if not team_name or not team_name.strip():
            results.append(ValidationResult(
                success=False,
                param_name="team_name",
                command_name=self.command_name,
                message=(
                    "team_name is empty. get_sports requires a specific team name "
                    "(e.g., 'Lakers', 'Giants', 'Yankees'). This query may be "
                    "better handled by a different tool."
                ),
            ))
            return results

        try:
            espn_service = ESPNSportsService()
            teams = espn_service.resolve_team(team_name.strip())
            if not teams:
                results.append(ValidationResult(
                    success=False,
                    param_name="team_name",
                    command_name=self.command_name,
                    message=(
                        f"'{team_name}' did not match any known sports team. "
                        "get_sports requires an actual team name "
                        "(e.g., 'Lakers', 'Giants', 'Yankees'). This query may be "
                        "better handled by a different tool."
                    ),
                ))
        except Exception:
            pass

        return results

    def run(self, request_info: RequestInformation, **kwargs) -> CommandResponse:
        team_name: str | None = kwargs.get("team_name")
        resolved_datetimes: list[str] | None = kwargs.get("resolved_datetimes")
        voice_command: str = request_info.voice_command

        if not team_name:
            return CommandResponse.error_response(
                error_details="Missing team_name parameter",
                context_data={"voice_command": voice_command, "error": "Missing team name"},
            )

        if not resolved_datetimes:
            return CommandResponse.error_response(
                error_details="Missing resolved_datetimes parameter",
                context_data={"voice_command": voice_command, "team_name": team_name, "error": "Missing dates"},
            )

        try:
            espn_service = ESPNSportsService()
            teams = espn_service.resolve_team(team_name)

            if not teams:
                return CommandResponse.error_response(
                    error_details=f"No teams found for: {team_name}",
                    context_data={"voice_command": voice_command, "team_name": team_name, "teams_found": 0},
                )

            # Fetch games for every requested date
            all_games: list[Game] = []
            dates_checked: list[str] = []

            for date_key in resolved_datetimes:
                espn_date = self._to_espn_date(date_key)
                dates_checked.append(date_key)
                for team in teams:
                    try:
                        team_games = espn_service.get_team_scores(team_name, espn_date)
                        all_games.extend(team_games)
                    except Exception:
                        continue

            # Deduplicate by game id
            seen_ids: set[str] = set()
            unique_games: list[Game] = []
            for game in all_games:
                if game.id not in seen_ids:
                    seen_ids.add(game.id)
                    unique_games.append(game)
            all_games = unique_games

            # If no games found, scan forward up to 7 days for the next game
            next_event = None
            if not all_games:
                next_event = self._find_next_event(teams, espn_service)

            if not all_games and not next_event:
                return CommandResponse.follow_up_response(
                    context_data={
                        "voice_command": voice_command,
                        "team_name": team_name,
                        "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                        "games_found": 0,
                        "dates_checked": dates_checked,
                    },
                )

            # Build context data for the LLM to craft a natural response
            games_data = self._format_games(all_games, team_name)

            context: dict[str, Any] = {
                "voice_command": voice_command,
                "team_name": team_name,
                "teams_resolved": [{"name": t.full_name, "league": t.league.value} for t in teams],
                "games_found": len(all_games),
                "games_data": games_data,
                "dates_checked": dates_checked,
            }

            if next_event:
                context["next_event"] = next_event
                context["games_found"] = 1

            return CommandResponse.follow_up_response(context_data=context)

        except Exception as e:
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={"voice_command": voice_command, "team_name": team_name, "error": str(e)},
            )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_espn_date(date_key: str) -> str:
        """Convert a date key to ESPN YYYYMMDD format.

        Handles:
        - ISO datetimes: '2026-03-12T04:00:00Z' → '20260312'
        - ISO dates: '2026-03-12' → '20260312'
        - Already YYYYMMDD: '20260312' → '20260312'
        - Unresolved relative keys ('tonight', 'today') → today's date
        """
        if not date_key:
            return datetime.now().strftime("%Y%m%d")

        # ISO datetime (contains 'T')
        if "T" in date_key:
            try:
                from utils.timezone_util import convert_utc_to_local
                local_dt = convert_utc_to_local(date_key)
                if local_dt:
                    return local_dt.strftime("%Y%m%d")
            except Exception:
                pass
            # Fallback: naive parse
            try:
                normalized = date_key.replace("Z", "+00:00")
                dt = datetime.fromisoformat(normalized)
                return dt.strftime("%Y%m%d")
            except ValueError:
                pass

        # ISO date (YYYY-MM-DD)
        if len(date_key) == 10 and date_key[4] == "-" and date_key[7] == "-":
            return date_key.replace("-", "")

        # Already YYYYMMDD
        if len(date_key) == 8 and date_key.isdigit():
            return date_key

        # Unresolved relative key — default to today
        return datetime.now().strftime("%Y%m%d")

    def _find_next_event(self, teams, espn_service: ESPNSportsService) -> dict | None:
        """Scan forward up to 7 days to find the next game for these teams."""
        today = datetime.now()
        for day_offset in range(1, 8):
            check_date = today + timedelta(days=day_offset)
            date_str = check_date.strftime("%Y%m%d")

            for team in teams:
                try:
                    games = espn_service.get_team_scores(team.nickname, date_str)
                    if games:
                        game = games[0]
                        return {
                            "away_team": game.away_team,
                            "home_team": game.home_team,
                            "status": game.status,
                            "league": game.league.value.upper(),
                            "venue": game.venue,
                            "broadcast": game.broadcast,
                            "start_time": format_datetime_local(game.start_time, "%I:%M %p") if game.start_time else None,
                            "date": check_date.strftime("%A, %B %d"),
                            "date_iso": check_date.strftime("%Y-%m-%d"),
                            "days_from_now": day_offset,
                        }
                except Exception:
                    continue

        return None

    def _format_games(self, games: list[Game], team_name: str) -> list[dict[str, Any]]:
        """Format Game objects into dicts for context_data."""
        formatted: list[dict[str, Any]] = []
        for game in games:
            is_home = team_name.lower() in game.home_team.lower()
            opponent = game.away_team if is_home else game.home_team

            entry: dict[str, Any] = {
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
                "date": game.start_time.strftime("%Y-%m-%d") if game.start_time else None,
            }
            if game.start_time:
                entry["start_time"] = format_datetime_local(game.start_time, "%I:%M %p")
            formatted.append(entry)

        return formatted
