from typing import List, Any, Optional
import requests
from abc import ABC, abstractmethod

from pydantic import BaseModel
from clients.responses.jarvis_command_center import DateContext
from core.ijarvis_command import IJarvisCommand, CommandExample, CommandAntipattern
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.command_response import CommandResponse
from services.secret_service import get_secret_value


class SearchResult(BaseModel):
    """Model for search results"""
    title: str
    snippet: str
    url: str


class SearchService(ABC):
    """Abstract base class for search services"""
    
    @abstractmethod
    def search(self, query: str) -> List[SearchResult]:
        """Perform a search and return results"""
        pass


class BingSearchService(SearchService):
    """Bing Web Search API service"""
    
    def __init__(self, api_key: str, region: str = "en-US"):
        self.api_key = api_key
        self.region = region
        self.base_url = "https://api.bing.microsoft.com/v7.0/search"
    
    def search(self, query: str) -> List[SearchResult]:
        """Perform Bing web search"""
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "X-MSEdge-ClientID": "jarvis-search-client"
        }
        
        params = {
            "q": query,
            "mkt": self.region,
            "count": 5,  # Limit results
            "safeSearch": "Moderate"
        }
        
        try:
            response = requests.get(self.base_url, headers=headers, params=params)
            response.raise_for_status()
            data = response.json()
            
            results = []
            if "webPages" in data and "value" in data["webPages"]:
                for item in data["webPages"]["value"][:5]:  # Top 5 results
                    results.append(SearchResult(
                        title=item.get("name", ""),
                        snippet=item.get("snippet", ""),
                        url=item.get("url", "")
                    ))
            
            return results
            
        except requests.RequestException as e:
            raise Exception(f"Bing search API error: {str(e)}")


class DuckDuckGoSearchService(SearchService):
    """DuckDuckGo Instant Answer API service"""
    
    def __init__(self):
        self.base_url = "https://api.duckduckgo.com/"
    
    def search(self, query: str) -> List[SearchResult]:
        """Perform DuckDuckGo instant answer search"""
        params = {
            "q": query,
            "format": "json",
            "no_html": "1",
            "skip_disambig": "1"
        }
        
        try:
            response = requests.get(self.base_url, params=params)
            response.raise_for_status()
            data = response.json()
            
            results = []
            
            # Try instant answer first
            if data.get("Answer"):
                results.append(SearchResult(
                    title="Instant Answer",
                    snippet=data["Answer"],
                    url=data.get("AnswerURL", "")
                ))
            
            # Try abstract
            elif data.get("Abstract"):
                results.append(SearchResult(
                    title=data.get("Heading", ""),
                    snippet=data["Abstract"],
                    url=data.get("AbstractURL", "")
                ))
            
            # Try related topics
            elif data.get("RelatedTopics"):
                for topic in data["RelatedTopics"][:3]:  # Top 3
                    if isinstance(topic, dict) and topic.get("Text"):
                        results.append(SearchResult(
                            title=topic.get("FirstURL", {}).get("text", "Related"),
                            snippet=topic["Text"],
                            url=topic.get("FirstURL", {}).get("url", "")
                        ))
            
            return results
            
        except requests.RequestException as e:
            raise Exception(f"DuckDuckGo search API error: {str(e)}")


class SearchServiceFactory:
    """Factory for creating search service instances"""
    
    @staticmethod
    def create_service() -> SearchService:
        """Create a search service based on configuration"""
        provider = get_secret_value("LIVE_SEARCH_PROVIDER", "integration")
        
        if provider == "bing":
            api_key = get_secret_value("LIVE_SEARCH_API_KEY", "integration")
            region = get_secret_value("LIVE_SEARCH_REGION", "integration")
            
            if not api_key:
                raise ValueError("Bing search requires LIVE_SEARCH_API_KEY to be set")
            if not region:
                raise ValueError("Bing search requires LIVE_SEARCH_REGION to be set")
            
            return BingSearchService(api_key, region)
        
        elif provider == "duckduckgo":
            return DuckDuckGoSearchService()
        
        else:
            raise ValueError(f"Unsupported search provider: {provider}. Supported providers: 'bing', 'duckduckgo'")


class WebSearchCommand(IJarvisCommand):
    """Command for performing live web searches"""

    @property
    def command_name(self) -> str:
        return "search_web"

    @property
    def keywords(self) -> List[str]:
        return [
            "search", "look up", "find", "current", "latest", "recent",
            "live", "real time", "news", "now", "today's news", "breaking"
        ]

    @property
    def description(self) -> str:
        return "Perform a live web search for current information, stock prices, news, election results, or any real-time data. Use for dynamic, changing information."

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use this for up-to-date or real-time info (news, stock moves, live scores, ongoing events).",
            "Use this for championship winners or season outcomes (e.g., 'Who won the Super Bowl this year?').",
            "Always call this tool for web search queries; do NOT answer directly from memory or ask clarifying questions first.",
            "Do NOT use this for stable facts or geography/location definitions.",
            "Only use this for questions that require up-to-date information.",
            "For 'what time is it in [location]' queries, use get_current_time instead."
        ]

    @property
    def antipatterns(self) -> List[CommandAntipattern]:
        return [
            CommandAntipattern(
                command_name="answer_question",
                description="Stable facts, definitions, biographies, geography, historical dates, or established knowledge."
            ),
            CommandAntipattern(
                command_name="get_sports_schedule",
                description="Upcoming games, schedules, future matchups, 'when do they play next'."
            ),
            CommandAntipattern(
                command_name="get_weather",
                description="Weather conditions, temperature, forecasts, rain, wind, or 'what's the weather'. Always use get_weather for weather queries."
            ),
            CommandAntipattern(
                command_name="get_sports_scores",
                description="Game scores, results, 'how did [team] do', 'what was the score', 'did [team] win', final scores."
            ),
            CommandAntipattern(
                command_name="get_current_time",
                description="Current time in a location, 'what time is it in [city/country]', time zone queries."
            ),
        ]

    def generate_prompt_examples(self) -> List[CommandExample]:
        """Generate concise example utterances with expected parameters using date context"""
        return [
            CommandExample(
                voice_command="Who won the senate race in Pennsylvania?",
                expected_parameters={"query": "Who won the senate race in Pennsylvania?"},
                is_primary=True
            ),
            CommandExample(
                voice_command="When is the next SpaceX launch?",
                expected_parameters={"query": "When is the next SpaceX launch?"}
            ),
            CommandExample(
                voice_command="Who won the Super Bowl this year?",
                expected_parameters={"query": "Who won the Super Bowl this year?"}
            ),
            CommandExample(
                voice_command="What's the latest news about Tesla?",
                expected_parameters={"query": "What's the latest news about Tesla?"}
            ),
            CommandExample(
                voice_command="Find the latest information about COVID vaccines",
                expected_parameters={"query": "Find the latest information about COVID vaccines"}
            )
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        """Generate varied examples for adapter training.

        Focus areas:
        - Championship winners - "Who won the Super Bowl/World Series?"
        - Election results, news, stocks
        - Explicit search triggers
        - Upcoming public events (product launches, etc.)

        NOTE: Time queries ("What time is it in X?") now go to get_current_time command.
        """
        examples = [
            # === CHAMPIONSHIP / SEASON WINNERS ===
            CommandExample(voice_command="Who won the Super Bowl this year?", expected_parameters={"query": "Who won the Super Bowl this year?"}, is_primary=True),
            CommandExample(voice_command="Who won the Super Bowl?", expected_parameters={"query": "Who won the Super Bowl?"}, is_primary=False),
            CommandExample(voice_command="Who won the World Series?", expected_parameters={"query": "Who won the World Series?"}, is_primary=False),
            CommandExample(voice_command="Who won the NBA Finals?", expected_parameters={"query": "Who won the NBA Finals?"}, is_primary=False),
            CommandExample(voice_command="Who won the Stanley Cup?", expected_parameters={"query": "Who won the Stanley Cup?"}, is_primary=False),

            # === ELECTION RESULTS ===
            CommandExample(voice_command="Who won the senate race in Pennsylvania?", expected_parameters={"query": "Who won the senate race in Pennsylvania?"}, is_primary=False),
            CommandExample(voice_command="Who won the presidential election?", expected_parameters={"query": "Who won the presidential election?"}, is_primary=False),

            # === NEWS / CURRENT EVENTS ===
            CommandExample(voice_command="What's the latest news about Tesla?", expected_parameters={"query": "What's the latest news about Tesla?"}, is_primary=False),
            CommandExample(voice_command="What's happening with the wildfires?", expected_parameters={"query": "What's happening with the wildfires?"}, is_primary=False),

            # === STOCK / FINANCIAL ===
            CommandExample(voice_command="What's the Tesla stock price?", expected_parameters={"query": "What's the Tesla stock price?"}, is_primary=False),
            CommandExample(voice_command="Current Bitcoin price", expected_parameters={"query": "Current Bitcoin price"}, is_primary=False),

            # === UPCOMING PUBLIC EVENTS ===
            CommandExample(voice_command="When is the next SpaceX launch?", expected_parameters={"query": "When is the next SpaceX launch?"}, is_primary=False),
            CommandExample(voice_command="When is the next iPhone release?", expected_parameters={"query": "When is the next iPhone release?"}, is_primary=False),

            # === EXPLICIT SEARCH TRIGGERS ===
            CommandExample(voice_command="Search for breaking news about AI", expected_parameters={"query": "Search for breaking news about AI"}, is_primary=False),
            CommandExample(voice_command="Look up the lottery numbers", expected_parameters={"query": "Look up the lottery numbers"}, is_primary=False),
        ]
        return examples
    
    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter("query", "string", required=True, 
                          description="Search query for current or recent information.")
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret("LIVE_SEARCH_PROVIDER", "Search provider: 'bing' or 'duckduckgo'", "integration", "string", required=True),
            JarvisSecret("LIVE_SEARCH_API_KEY", "API Key for the selected search provider (not needed for DuckDuckGo)", "integration", "string", required=False),  
            JarvisSecret("LIVE_SEARCH_REGION", "Search region/locale (e.g. 'en-US') - used by some providers", "integration", "string", required=False),
        ]

    @property
    def critical_rules(self) -> List[str]:
        return [
            "Use this command for questions requiring CURRENT, LIVE, or UP-TO-DATE information",
            "This command performs actual web searches - use for recent events, current data, real-time information",
            "Do NOT use this for established facts, historical information, or general knowledge that doesn't change"
        ]

    def run(self, request_info, **kwargs) -> CommandResponse:
        query = kwargs.get("query")
        
        if not query:
            return CommandResponse.error_response(
                                error_details="Search query parameter is required",
                context_data={
                    "query": None,
                    "error": "Query parameter is required"
                }
            )
        
        try:
            # Create search service based on provider
            search_service = SearchServiceFactory.create_service()
            
            # Perform the search
            search_results = search_service.search(query)
            
            if not search_results:
                return CommandResponse.follow_up_response(
                                        context_data={
                        "query": query,
                        "results_found": 0,
                        "provider": get_secret_value("LIVE_SEARCH_PROVIDER", "integration")
                    }
                )
            
            # Return raw search results - server will format the response
            return CommandResponse.success_response(
                context_data={
                    "query": query,
                    "results_found": len(search_results),
                    "provider": get_secret_value("LIVE_SEARCH_PROVIDER", "integration"),
                    "search_results": [
                        {
                            "title": result.title,
                            "snippet": result.snippet,
                            "url": result.url
                        } for result in search_results[:5]  # Top 5 results
                    ]
                }
            )
            
        except Exception as e:
            # Return error
            return CommandResponse.error_response(
                error_details=str(e),
                context_data={
                        "query": query,
                        "results_found": len(search_results),
                        "provider": get_secret_value("LIVE_SEARCH_PROVIDER", "integration"),
                        "llm_error": str(e),
                        "search_results": [
                            {
                                "title": result.title,
                                "snippet": result.snippet,
                                "url": result.url
                            } for result in search_results
                        ]
                    }
                )
        
        except ValueError as e:
            # Provider configuration error
            return CommandResponse.error_response(
                                error_details=str(e),
                context_data={
                    "query": query,
                    "error_type": "configuration_error",
                    "error": str(e)
                }
            )
        
        except Exception as e:
            # General search error
            return CommandResponse.error_response(
                                error_details=str(e),
                context_data={
                    "query": query,
                    "error_type": "search_error",
                    "error": str(e)
                }
            )
