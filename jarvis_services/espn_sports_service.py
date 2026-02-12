from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional

import requests


class League(Enum):
    """Supported sports leagues"""
    NFL = "nfl"
    NBA = "nba"
    MLB = "mlb"
    NHL = "nhl"
    COLLEGE_FOOTBALL = "college-football"
    COLLEGE_BASKETBALL = "college-basketball"


@dataclass
class Team:
    """Represents a sports team"""
    league: League
    city: str
    nickname: str
    full_name: str
    espn_id: Optional[str] = None
    
    def __post_init__(self):
        if not self.full_name:
            self.full_name = f"{self.city} {self.nickname}"


class TeamNameResolver:
    """Resolves team names to actual teams across all leagues"""
    
    def __init__(self):
        self._teams = self._build_team_database()
        self._nickname_aliases = self._build_nickname_aliases()
    
    def _build_team_database(self) -> List[Team]:
        """Build the complete team database from all leagues"""
        teams = []
        
        # NFL Teams
        nfl_teams = [
            ("Buffalo", "Bills"), ("Miami", "Dolphins"), ("New England", "Patriots"), ("New York", "Jets"),
            ("Baltimore", "Ravens"), ("Cincinnati", "Bengals"), ("Cleveland", "Browns"), ("Pittsburgh", "Steelers"),
            ("Houston", "Texans"), ("Indianapolis", "Colts"), ("Jacksonville", "Jaguars"), ("Tennessee", "Titans"),
            ("Denver", "Broncos"), ("Kansas City", "Chiefs"), ("Las Vegas", "Raiders"), ("Los Angeles", "Chargers"),
            ("Dallas", "Cowboys"), ("New York", "Giants"), ("Philadelphia", "Eagles"), ("Washington", "Commanders"),
            ("Chicago", "Bears"), ("Detroit", "Lions"), ("Green Bay", "Packers"), ("Minnesota", "Vikings"),
            ("Atlanta", "Falcons"), ("Carolina", "Panthers"), ("New Orleans", "Saints"), ("Tampa Bay", "Buccaneers"),
            ("Arizona", "Cardinals"), ("Los Angeles", "Rams"), ("San Francisco", "49ers"), ("Seattle", "Seahawks")
        ]
        
        for city, nickname in nfl_teams:
            teams.append(Team(League.NFL, city, nickname, f"{city} {nickname}"))
        
        # NBA Teams
        nba_teams = [
            ("Boston", "Celtics"), ("Brooklyn", "Nets"), ("New York", "Knicks"), ("Philadelphia", "76ers"), ("Toronto", "Raptors"),
            ("Chicago", "Bulls"), ("Cleveland", "Cavaliers"), ("Detroit", "Pistons"), ("Indiana", "Pacers"), ("Milwaukee", "Bucks"),
            ("Atlanta", "Hawks"), ("Charlotte", "Hornets"), ("Miami", "Heat"), ("Orlando", "Magic"), ("Washington", "Wizards"),
            ("Golden State", "Warriors"), ("Los Angeles", "Clippers"), ("Los Angeles", "Lakers"), ("Phoenix", "Suns"), ("Sacramento", "Kings"),
            ("Denver", "Nuggets"), ("Minnesota", "Timberwolves"), ("Oklahoma City", "Thunder"), ("Portland", "Trail Blazers"), ("Utah", "Jazz"),
            ("Dallas", "Mavericks"), ("Houston", "Rockets"), ("Memphis", "Grizzlies"), ("New Orleans", "Pelicans"), ("San Antonio", "Spurs")
        ]
        
        for city, nickname in nba_teams:
            teams.append(Team(League.NBA, city, nickname, f"{city} {nickname}"))
        
        # MLB Teams
        mlb_teams = [
            ("Baltimore", "Orioles"), ("Boston", "Red Sox"), ("New York", "Yankees"), ("Tampa Bay", "Rays"), ("Toronto", "Blue Jays"),
            ("Chicago", "White Sox"), ("Cleveland", "Guardians"), ("Detroit", "Tigers"), ("Kansas City", "Royals"), ("Minnesota", "Twins"),
            ("Houston", "Astros"), ("Los Angeles", "Angels"), ("Oakland", "Athletics"), ("Seattle", "Mariners"), ("Texas", "Rangers"),
            ("Atlanta", "Braves"), ("Miami", "Marlins"), ("New York", "Mets"), ("Philadelphia", "Phillies"), ("Washington", "Nationals"),
            ("Chicago", "Cubs"), ("Cincinnati", "Reds"), ("Milwaukee", "Brewers"), ("Pittsburgh", "Pirates"), ("St. Louis", "Cardinals"),
            ("Arizona", "Diamondbacks"), ("Colorado", "Rockies"), ("Los Angeles", "Dodgers"), ("San Diego", "Padres"), ("San Francisco", "Giants")
        ]
        
        for city, nickname in mlb_teams:
            teams.append(Team(League.MLB, city, nickname, f"{city} {nickname}"))
        
        # NHL Teams
        nhl_teams = [
            ("Boston", "Bruins"), ("Buffalo", "Sabres"), ("Detroit", "Red Wings"), ("Florida", "Panthers"), ("Montreal", "Canadiens"),
            ("Ottawa", "Senators"), ("Tampa Bay", "Lightning"), ("Toronto", "Maple Leafs"), ("Carolina", "Hurricanes"), ("Columbus", "Blue Jackets"),
            ("New Jersey", "Devils"), ("New York", "Islanders"), ("New York", "Rangers"), ("Philadelphia", "Flyers"), ("Pittsburgh", "Penguins"),
            ("Washington", "Capitals"), ("Arizona", "Coyotes"), ("Chicago", "Blackhawks"), ("Colorado", "Avalanche"), ("Dallas", "Stars"),
            ("Minnesota", "Wild"), ("Nashville", "Predators"), ("St. Louis", "Blues"), ("Winnipeg", "Jets"), ("Anaheim", "Ducks"),
            ("Calgary", "Flames"), ("Edmonton", "Oilers"), ("Los Angeles", "Kings"), ("San Jose", "Sharks"), ("Seattle", "Kraken"),
            ("Vancouver", "Canucks"), ("Vegas", "Golden Knights")
        ]
        
        for city, nickname in nhl_teams:
            teams.append(Team(League.NHL, city, nickname, f"{city} {nickname}"))
        
        # College Football Teams (Power 5 + Big East)
        college_football_teams = [
            # SEC
            ("Alabama", "Crimson Tide"), ("Auburn", "Tigers"), ("Florida", "Gators"), ("Georgia", "Bulldogs"),
            ("Kentucky", "Wildcats"), ("LSU", "Tigers"), ("Mississippi State", "Bulldogs"), ("Missouri", "Tigers"),
            ("Ole Miss", "Rebels"), ("South Carolina", "Gamecocks"), ("Tennessee", "Volunteers"), ("Texas A&M", "Aggies"),
            ("Arkansas", "Razorbacks"), ("Vanderbilt", "Commodores"),
            
            # Big Ten
            ("Michigan", "Wolverines"), ("Michigan State", "Spartans"), ("Ohio State", "Buckeyes"), ("Penn State", "Nittany Lions"),
            ("Indiana", "Hoosiers"), ("Maryland", "Terrapins"), ("Rutgers", "Scarlet Knights"), ("Illinois", "Fighting Illini"),
            ("Iowa", "Hawkeyes"), ("Minnesota", "Golden Gophers"), ("Nebraska", "Cornhuskers"), ("Northwestern", "Wildcats"),
            ("Purdue", "Boilermakers"), ("Wisconsin", "Badgers"),
            
            # ACC
            ("Boston College", "Eagles"), ("Clemson", "Tigers"), ("Duke", "Blue Devils"), ("Florida State", "Seminoles"),
            ("Georgia Tech", "Yellow Jackets"), ("Louisville", "Cardinals"), ("Miami", "Hurricanes"), ("North Carolina", "Tar Heels"),
            ("NC State", "Wolfpack"), ("Pittsburgh", "Panthers"), ("Syracuse", "Orange"), ("Virginia", "Cavaliers"),
            ("Virginia Tech", "Hokies"), ("Wake Forest", "Demon Deacons"),
            
            # Big 12
            ("Baylor", "Bears"), ("Iowa State", "Cyclones"), ("Kansas", "Jayhawks"), ("Kansas State", "Wildcats"),
            ("Oklahoma", "Sooners"), ("Oklahoma State", "Cowboys"), ("TCU", "Horned Frogs"), ("Texas", "Longhorns"),
            ("Texas Tech", "Red Raiders"), ("West Virginia", "Mountaineers"),
            
            # Pac-12
            ("Arizona", "Wildcats"), ("Arizona State", "Sun Devils"), ("California", "Golden Bears"), ("Colorado", "Buffaloes"),
            ("Oregon", "Ducks"), ("Oregon State", "Beavers"), ("Stanford", "Cardinal"), ("UCLA", "Bruins"),
            ("USC", "Trojans"), ("Utah", "Utes"), ("Washington", "Huskies"), ("Washington State", "Cougars"),
            
            # Big East (Basketball, but some have football)
            ("Villanova", "Wildcats"), ("Georgetown", "Hoyas"), ("St. John's", "Red Storm"), ("Seton Hall", "Pirates"),
            ("Providence", "Friars"), ("DePaul", "Blue Demons"), ("Marquette", "Golden Eagles"), ("Xavier", "Musketeers"),
            ("Butler", "Bulldogs"), ("Creighton", "Bluejays"), ("UConn", "Huskies")
        ]
        
        for city, nickname in college_football_teams:
            teams.append(Team(League.COLLEGE_FOOTBALL, city, nickname, f"{city} {nickname}"))
        
        # College Basketball Teams (Power 5 + Big East)
        college_basketball_teams = [
            # Add major basketball programs from the same conferences
            # Many overlap with football, but some are basketball-only
            ("Gonzaga", "Bulldogs"), ("Memphis", "Tigers"), ("Houston", "Cougars"), ("Cincinnati", "Bearcats"),
            ("Wichita State", "Shockers"), ("Dayton", "Flyers"), ("Saint Mary's", "Gaels"), ("BYU", "Cougars"),
            ("San Diego State", "Aztecs"), ("Boise State", "Broncos"), ("Nevada", "Wolf Pack"), ("UNLV", "Runnin' Rebels")
        ]
        
        for city, nickname in college_basketball_teams:
            teams.append(Team(League.COLLEGE_BASKETBALL, city, nickname, f"{city} {nickname}"))
        
        return teams
    
    def _build_nickname_aliases(self) -> Dict[str, List[str]]:
        """Build common nickname aliases for teams"""
        return {
            "yanks": ["Yankees"],
            "bombers": ["Yankees"],
            "bronx bombers": ["Yankees"],
            "celtics": ["Celtics"],
            "celts": ["Celtics"],
            "c's": ["Celtics"],
            "lakers": ["Lakers"],
            "lake show": ["Lakers"],
            "warriors": ["Warriors"],
            "dubs": ["Warriors"],
            "heat": ["Heat"],
            "knicks": ["Knicks"],
            "nets": ["Nets"],
            "sixers": ["76ers"],
            "76ers": ["76ers"],
            "raptors": ["Raptors"],
            "raps": ["Raptors"],
            "bulls": ["Bulls"],
            "cavs": ["Cavaliers"],
            "cavaliers": ["Cavaliers"],
            "pistons": ["Pistons"],
            "pacers": ["Pacers"],
            "bucks": ["Bucks"],
            "hawks": ["Hawks"],
            "hornets": ["Hornets"],
            "magic": ["Magic"],
            "wizards": ["Wizards"],
            "clippers": ["Clippers"],
            "clips": ["Clippers"],
            "suns": ["Suns"],
            "kings": ["Kings"],
            "nuggets": ["Nuggets"],
            "nugs": ["Nuggets"],
            "timberwolves": ["Timberwolves"],
            "wolves": ["Timberwolves"],
            "thunder": ["Thunder"],
            "okc": ["Thunder"],
            "trail blazers": ["Trail Blazers"],
            "blazers": ["Trail Blazers"],
            "jazz": ["Jazz"],
            "mavs": ["Mavericks"],
            "mavericks": ["Mavericks"],
            "rockets": ["Rockets"],
            "grizzlies": ["Grizzlies"],
            "grizz": ["Grizzlies"],
            "pelicans": ["Pelicans"],
            "pels": ["Pelicans"],
            "spurs": ["Spurs"],
            "bills": ["Bills"],
            "dolphins": ["Dolphins"],
            "fins": ["Dolphins"],
            "patriots": ["Patriots"],
            "pats": ["Patriots"],
            "jets": ["Jets"],
            "ravens": ["Ravens"],
            "bengals": ["Bengals"],
            "browns": ["Browns"],
            "steelers": ["Steelers"],
            "texans": ["Texans"],
            "colts": ["Colts"],
            "jaguars": ["Jaguars"],
            "jags": ["Jaguars"],
            "titans": ["Titans"],
            "broncos": ["Broncos"],
            "chiefs": ["Chiefs"],
            "raiders": ["Raiders"],
            "chargers": ["Chargers"],
            "bolts": ["Chargers"],
            "cowboys": ["Cowboys"],
            "boys": ["Cowboys"],
            "giants": ["Giants"],
            "big blue": ["Giants"],
            "eagles": ["Eagles"],
            "birds": ["Eagles"],
            "commanders": ["Commanders"],
            "skins": ["Commanders"],
            "redskins": ["Commanders"],
            "bears": ["Bears"],
            "lions": ["Lions"],
            "packers": ["Packers"],
            "pack": ["Packers"],
            "vikings": ["Vikings"],
            "vikes": ["Vikings"],
            "falcons": ["Falcons"],
            "dirty birds": ["Falcons"],
            "panthers": ["Panthers"],
            "saints": ["Saints"],
            "who dat": ["Saints"],
            "buccaneers": ["Buccaneers"],
            "bucs": ["Buccaneers"],
            "cardinals": ["Cardinals"],
            "cards": ["Cardinals"],
            "rams": ["Rams"],
            "49ers": ["49ers"],
            "niners": ["49ers"],
            "seahawks": ["Seahawks"],
            "hawks": ["Seahawks"],
            "orioles": ["Orioles"],
            "o's": ["Orioles"],
            "red sox": ["Red Sox"],
            "sox": ["Red Sox"],
            "rays": ["Rays"],
            "blue jays": ["Blue Jays"],
            "jays": ["Blue Jays"],
            "white sox": ["White Sox"],
            "guardians": ["Guardians"],
            "tigers": ["Tigers"],
            "royals": ["Royals"],
            "twins": ["Twins"],
            "astros": ["Astros"],
            "stros": ["Astros"],
            "angels": ["Angels"],
            "halos": ["Angels"],
            "athletics": ["Athletics"],
            "a's": ["Athletics"],
            "mariners": ["Mariners"],
            "ms": ["Mariners"],
            "rangers": ["Rangers"],
            "braves": ["Braves"],
            "marlins": ["Marlins"],
            "mets": ["Mets"],
            "amazins": ["Mets"],
            "phillies": ["Phils"],
            "phils": ["Phils"],
            "nationals": ["Nationals"],
            "nats": ["Nationals"],
            "cubs": ["Cubs"],
            "reds": ["Reds"],
            "brewers": ["Brewers"],
            "brew crew": ["Brewers"],
            "pirates": ["Pirates"],
            "bucs": ["Pirates"],
            "cardinals": ["Cardinals"],
            "birds": ["Cardinals"],
            "diamondbacks": ["Diamondbacks"],
            "dbacks": ["Diamondbacks"],
            "snakes": ["Diamondbacks"],
            "rockies": ["Rockies"],
            "dodgers": ["Dodgers"],
            "blue crew": ["Dodgers"],
            "padres": ["Padres"],
            "friars": ["Padres"],
            "giants": ["Giants"],
            "orange and black": ["Giants"],
            "bruins": ["Bruins"],
            "sabres": ["Sabres"],
            "red wings": ["Red Wings"],
            "wings": ["Red Wings"],
            "canadiens": ["Canadiens"],
            "habs": ["Canadiens"],
            "senators": ["Senators"],
            "sens": ["Senators"],
            "lightning": ["Lightning"],
            "bolts": ["Lightning"],
            "maple leafs": ["Maple Leafs"],
            "leafs": ["Maple Leafs"],
            "hurricanes": ["Hurricanes"],
            "canes": ["Hurricanes"],
            "blue jackets": ["Blue Jackets"],
            "jackets": ["Blue Jackets"],
            "devils": ["Devils"],
            "islanders": ["Isles"],
            "isles": ["Isles"],
            "rangers": ["Rangers"],
            "flyers": ["Flyers"],
            "penguins": ["Pens"],
            "pens": ["Penguins"],
            "capitals": ["Caps"],
            "caps": ["Capitals"],
            "coyotes": ["Yotes"],
            "yotes": ["Coyotes"],
            "blackhawks": ["Hawks"],
            "hawks": ["Blackhawks"],
            "avalanche": ["Avs"],
            "avs": ["Avalanche"],
            "stars": ["Stars"],
            "wild": ["Wild"],
            "predators": ["Preds"],
            "preds": ["Predators"],
            "blues": ["Blues"],
            "jets": ["Jets"],
            "ducks": ["Ducks"],
            "flames": ["Flames"],
            "oilers": ["Oilers"],
            "kings": ["Kings"],
            "sharks": ["Sharks"],
            "kraken": ["Kraken"],
            "canucks": ["Canucks"],
            "nucks": ["Canucks"],
            "golden knights": ["Golden Knights"],
            "knights": ["Golden Knights"],
            
            # College Football Nicknames
            "tide": ["Crimson Tide"], "bama": ["Crimson Tide"], "alabama": ["Crimson Tide"],
            "war eagle": ["Tigers"], "auburn": ["Tigers"],
            "gators": ["Gators"], "florida": ["Gators"],
            "dawgs": ["Bulldogs"], "georgia": ["Bulldogs"],
            "cats": ["Wildcats"], "kentucky": ["Wildcats"],
            "lsu": ["Tigers"], "tigers": ["Tigers"],
            "bulldogs": ["Bulldogs"], "mississippi state": ["Bulldogs"],
            "mizzou": ["Tigers"], "missouri": ["Tigers"],
            "ole miss": ["Rebels"], "rebels": ["Rebels"],
            "gamecocks": ["Gamecocks"], "south carolina": ["Gamecocks"],
            "vols": ["Volunteers"], "tennessee": ["Volunteers"],
            "aggies": ["Aggies"], "texas a&m": ["Aggies"],
            "razorbacks": ["Razorbacks"], "arkansas": ["Razorbacks"],
            "dores": ["Commodores"], "vanderbilt": ["Commodores"],
            
            # Big Ten
            "wolverines": ["Wolverines"], "michigan": ["Wolverines"],
            "spartans": ["Spartans"], "michigan state": ["Spartans"],
            "buckeyes": ["Buckeyes"], "ohio state": ["Buckeyes"],
            "nittany lions": ["Nittany Lions"], "penn state": ["Nittany Lions"],
            "hoosiers": ["Hoosiers"], "indiana": ["Hoosiers"],
            "terps": ["Terrapins"], "maryland": ["Terrapins"],
            "scarlet knights": ["Scarlet Knights"], "rutgers": ["Scarlet Knights"],
            "fighting illini": ["Fighting Illini"], "illinois": ["Fighting Illini"],
            "hawkeyes": ["Hawkeyes"], "iowa": ["Hawkeyes"],
            "golden gophers": ["Golden Gophers"], "minnesota": ["Golden Gophers"],
            "huskers": ["Cornhuskers"], "nebraska": ["Cornhuskers"],
            "wildcats": ["Wildcats"], "northwestern": ["Wildcats"],
            "boilermakers": ["Boilermakers"], "purdue": ["Boilermakers"],
            "badgers": ["Badgers"], "wisconsin": ["Badgers"],
            
            # ACC
            "eagles": ["Eagles"], "boston college": ["Eagles"],
            "clemson": ["Tigers"],
            "blue devils": ["Blue Devils"], "duke": ["Blue Devils"],
            "seminoles": ["Seminoles"], "florida state": ["Seminoles"],
            "yellow jackets": ["Yellow Jackets"], "georgia tech": ["Yellow Jackets"],
            "cardinals": ["Cardinals"], "louisville": ["Cardinals"],
            "hurricanes": ["Hurricanes"], "miami": ["Hurricanes"],
            "tar heels": ["Tar Heels"], "north carolina": ["Tar Heels"],
            "wolfpack": ["Wolfpack"], "nc state": ["Wolfpack"],
            "panthers": ["Panthers"], "pitt": ["Panthers"],
            "orange": ["Orange"], "syracuse": ["Orange"],
            "cavaliers": ["Cavaliers"], "virginia": ["Cavaliers"],
            "hokies": ["Hokies"], "virginia tech": ["Hokies"],
            "demon deacons": ["Demon Deacons"], "wake forest": ["Demon Deacons"],
            
            # Big 12
            "bears": ["Bears"], "baylor": ["Bears"],
            "cyclones": ["Cyclones"], "iowa state": ["Cyclones"],
            "jayhawks": ["Jayhawks"], "kansas": ["Jayhawks"],
            "wildcats": ["Wildcats"], "kansas state": ["Wildcats"],
            "sooners": ["Sooners"], "oklahoma": ["Sooners"],
            "cowboys": ["Cowboys"], "oklahoma state": ["Cowboys"],
            "horned frogs": ["Horned Frogs"], "tcu": ["Horned Frogs"],
            "longhorns": ["Longhorns"], "texas": ["Longhorns"],
            "red raiders": ["Red Raiders"], "texas tech": ["Red Raiders"],
            "mountaineers": ["Mountaineers"], "west virginia": ["Mountaineers"],
            
            # Pac-12
            "wildcats": ["Wildcats"], "arizona": ["Wildcats"],
            "sun devils": ["Sun Devils"], "arizona state": ["Sun Devils"],
            "golden bears": ["Golden Bears"], "cal": ["Golden Bears"], "california": ["Golden Bears"],
            "buffaloes": ["Buffaloes"], "colorado": ["Buffaloes"],
            "ducks": ["Ducks"], "oregon": ["Ducks"],
            "beavers": ["Beavers"], "oregon state": ["Beavers"],
            "cardinal": ["Cardinal"], "stanford": ["Cardinal"],
            "bruins": ["Bruins"], "ucla": ["Bruins"],
            "trojans": ["Trojans"], "usc": ["Trojans"],
            "utes": ["Utes"], "utah": ["Utes"],
            "huskies": ["Huskies"], "washington": ["Huskies"],
            "cougars": ["Cougars"], "washington state": ["Cougars"]
        }
    
    def resolve_team(self, user_input: str) -> List[Team]:
        """
        Resolve a user input to matching teams across all leagues
        Handles both simple nicknames and full team names with locality
        
        Args:
            user_input: User's team name input (e.g., "Giants", "Seattle Mariners", "New York Yankees")
            
        Returns:
            List of matching Team objects
        """
        if not user_input:
            return []
        
        # Normalize input
        normalized_input = user_input.lower().strip()
        
        # Try smart parsing first for multi-word team names
        if ' ' in normalized_input:
            matches = self._resolve_multi_word_team(normalized_input)
            if matches:
                return matches
        
        # Fall back to original logic for single words or if multi-word parsing fails
        return self._resolve_single_word_team(normalized_input)
    
    def _resolve_multi_word_team(self, team_input: str) -> List[Team]:
        """
        Resolve multi-word team names like "Seattle Mariners", "New York Yankees"
        """
        parts = team_input.split()
        
        # Try different combinations of locality + nickname
        for i in range(1, len(parts)):
            locality_part = ' '.join(parts[:i])
            nickname_part = ' '.join(parts[i:])
            
            # Look for teams that match this locality + nickname combination
            matches = []
            for team in self._teams:
                team_city_lower = team.city.lower()
                team_nickname_lower = team.nickname.lower()
                
                # Check if locality matches city and nickname matches
                if (locality_part in team_city_lower or team_city_lower in locality_part) and \
                   (nickname_part == team_nickname_lower or team_nickname_lower in nickname_part):
                    matches.append(team)
            
            if matches:
                return matches
        
        # If no locality/nickname split worked, try full name matching
        return self._resolve_single_word_team(team_input)
    
    def _resolve_single_word_team(self, normalized_input: str) -> List[Team]:
        """
        Original team resolution logic for single words or full names
        """
        # Check nickname aliases first
        if normalized_input in self._nickname_aliases:
            target_nicknames = self._nickname_aliases[normalized_input]
            matches = []
            for nickname in target_nicknames:
                matches.extend([team for team in self._teams if team.nickname.lower() == nickname.lower()])
            return matches
        
        # Direct nickname match
        direct_matches = [team for team in self._teams if team.nickname.lower() == normalized_input]
        if direct_matches:
            return direct_matches
        
        # Partial nickname match
        partial_matches = [team for team in self._teams if normalized_input in team.nickname.lower()]
        if partial_matches:
            return partial_matches
        
        # City match
        city_matches = [team for team in self._teams if normalized_input in team.city.lower()]
        if city_matches:
            return city_matches
        
        # Full name match
        full_name_matches = [team for team in self._teams if normalized_input in team.full_name.lower()]
        if full_name_matches:
            return full_name_matches
        
        return []
    
    def get_teams_by_league(self, league: League) -> List[Team]:
        """Get all teams for a specific league"""
        return [team for team in self._teams if team.league == league]
    
    def search_teams(self, query: str) -> List[Team]:
        """Search teams by any part of their name"""
        if not query:
            return []
        
        query = query.lower()
        matches = []
        
        for team in self._teams:
            if (query in team.city.lower() or 
                query in team.nickname.lower() or 
                query in team.full_name.lower()):
                matches.append(team)
        
        return matches


# NOTE: ESPN API returns all dates in UTC format (e.g., "2025-08-19T18:20Z")
# We convert these to the user's local timezone for display


@dataclass
class Game:
    """Represents a sports game"""
    id: str
    home_team: str
    away_team: str
    home_score: Optional[int]
    away_score: Optional[int]
    status: str  # "scheduled", "live", "final"
    start_time: Optional[datetime]
    league: League
    venue: Optional[str] = None
    broadcast: Optional[str] = None


# Main service class
class ESPNSportsService:
    """Main service for ESPN sports API interactions"""
    
    def __init__(self):
        self.team_resolver = TeamNameResolver()
        self.base_url = "http://site.api.espn.com/apis/site/v2/sports"
        self.session = requests.Session()
    
    def resolve_team(self, user_input: str) -> List[Team]:
        """Resolve team names using the team resolver"""
        return self.team_resolver.resolve_team(user_input)
    
    def get_scores(self, sport: League, date: Optional[str] = None) -> List[Game]:
        """
        Get scores for a specific sport and date
        
        Args:
            sport: League enum (NFL, NBA, MLB, NHL, COLLEGE_FOOTBALL, COLLEGE_BASKETBALL)
            date: Date string in YYYYMMDD format (default: today)
            
        Returns:
            List of Game objects
        """
        if date is None:
            date = datetime.now().strftime("%Y%m%d")
        
        # Map our League enum to ESPN API sport names
        sport_mapping = {
            League.NFL: "football/nfl",
            League.NBA: "basketball/nba", 
            League.MLB: "baseball/mlb",
            League.NHL: "hockey/nhl",
            League.COLLEGE_FOOTBALL: "football/college-football",
            League.COLLEGE_BASKETBALL: "basketball/mens-college-basketball"
        }
        
        if sport not in sport_mapping:
            raise ValueError(f"Unsupported sport: {sport}")
        
        espn_sport = sport_mapping[sport]
        url = f"{self.base_url}/{espn_sport}/scoreboard"
        
        params = {
            "dates": date,
            "calendar": "blacklist"  # ESPN API parameter
        }
        
        try:
            response = self.session.get(url, params=params, timeout=10)
            response.raise_for_status()
            
            data = response.json()
            return self._parse_scoreboard_response(data, sport)
            
        except requests.exceptions.RequestException as e:
            return []
        except Exception as e:
            return []
    
    def get_team_scores(self, team_input: str, date: Optional[str] = None) -> List[Game]:
        """
        Get scores for a specific team across all relevant sports
        
        Args:
            team_input: User's team name input (e.g., "Giants", "Panthers")
            date: Date string in YYYYMMDD format (default: today)
            
        Returns:
            List of Game objects for the team
        """
        # Resolve the team name to actual teams
        teams = self.team_resolver.resolve_team(team_input)
        
        if not teams:
            return []
        
        # Get scores for each league where the team exists
        all_games = []
        for team in teams:
            try:
                league_games = self.get_scores(team.league, date)
                # Filter games to only include the specific team
                team_games = []
                for game in league_games:
                    is_home = team.nickname in game.home_team
                    is_away = team.nickname in game.away_team
                    if is_home or is_away:
                        team_games.append(game)
                
                all_games.extend(team_games)
            except Exception as e:
                continue
        
        return all_games
    
    def _parse_scoreboard_response(self, data: dict, sport: League) -> List[Game]:
        """Parse ESPN API scoreboard response into Game objects"""
        games = []
        
        try:
            events = data.get("events", [])
            
            for event in events:
                try:
                    game_id = event.get("id", "")
                    status = event.get("status", {}).get("type", {}).get("name", "scheduled")
                    
                    # Get teams
                    competitions = event.get("competitions", [])
                    if not competitions:
                        continue
                    
                    competition = competitions[0]
                    competitors = competition.get("competitors", [])
                    
                    home_team = None
                    away_team = None
                    home_score = None
                    away_score = None
                    
                    for competitor in competitors:
                        team_name = competitor.get("team", {}).get("name", "")
                        score = competitor.get("score", "")
                        home_away = competitor.get("homeAway", "")
                        
                        
                        if home_away == "home":
                            home_team = team_name
                            home_score = int(score) if score.isdigit() else None
                        elif home_away == "away":
                            away_team = team_name
                            away_score = int(score) if score.isdigit() else None
                    
                    if not home_team or not away_team:
                        continue
                    
                    # Get start time and convert from UTC to local timezone
                    start_time = None
                    date_obj = event.get("date")
                    if date_obj:
                        try:
                            from utils.timezone_util import convert_utc_to_local
                            start_time = convert_utc_to_local(date_obj)
                        except Exception as parse_error:
                            pass
                    
                    # Get venue and broadcast info
                    venue = competition.get("venue", {}).get("fullName")
                    broadcast = None
                    broadcasts = competition.get("broadcasts", [])
                    if broadcasts:
                        broadcast = broadcasts[0].get("names", [""])[0]
                    
                    game = Game(
                        id=game_id,
                        home_team=home_team,
                        away_team=away_team,
                        home_score=home_score,
                        away_score=away_score,
                        status=status,
                        start_time=start_time,
                        league=sport,
                        venue=venue,
                        broadcast=broadcast
                    )
                    
                    games.append(game)
                    
                except Exception as e:
                    continue
            
        except Exception as e:
            pass
        
        return games
