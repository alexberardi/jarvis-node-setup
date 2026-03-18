"""NewsCommand — fetch RSS news headlines.

Returns structured article data for the LLM to compose into a spoken response,
or for use as a step in a briefing routine.
"""

import calendar
from typing import Any, Dict, List

import feedparser
from jarvis_log_client import JarvisLogger

from core.command_response import CommandResponse
from core.ijarvis_command import CommandExample, IJarvisCommand
from core.ijarvis_parameter import IJarvisParameter, JarvisParameter
from core.ijarvis_package import JarvisPackage
from core.ijarvis_secret import IJarvisSecret, JarvisSecret
from core.request_information import RequestInformation
from services.secret_service import get_secret_value

logger = JarvisLogger(service="jarvis-node")

_DEFAULT_FEEDS: Dict[str, List[str]] = {
    "general": [
        "https://feeds.apnews.com/rss/apf-topnews",
        "https://feeds.bbci.co.uk/news/rss.xml",
    ],
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://rss.nytimes.com/services/xml/rss/nyt/Technology.xml",
    ],
    "sports": [
        "https://www.espn.com/espn/rss/ncb/news",
        "https://feeds.apnews.com/rss/apf-sports",
    ],
    "business": [
        "https://feeds.reuters.com/reuters/businessNews",
    ],
    "science": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Science.xml",
    ],
    "health": [
        "https://rss.nytimes.com/services/xml/rss/nyt/Health.xml",
    ],
}

_CATEGORIES = list(_DEFAULT_FEEDS.keys())


class NewsCommand(IJarvisCommand):

    @property
    def command_name(self) -> str:
        return "get_news"

    @property
    def description(self) -> str:
        return "Get the latest news headlines by category. Supports general, tech, sports, business, science, and health."

    @property
    def keywords(self) -> List[str]:
        return ["news", "headlines", "briefing news", "what's happening", "current events"]

    @property
    def parameters(self) -> List[IJarvisParameter]:
        return [
            JarvisParameter(
                "category",
                "string",
                required=False,
                description="News category to fetch.",
                enum_values=_CATEGORIES,
            ),
            JarvisParameter(
                "count",
                "int",
                required=False,
                description="Number of headlines to return (default 5).",
            ),
        ]

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return [
            JarvisSecret(
                key="NEWS_RSS_FEEDS",
                description="Comma-separated custom RSS feed URLs (optional, merged with built-in feeds).",
                scope="integration",
                value_type="string",
                required=False,
                is_sensitive=False,
                friendly_name="Custom RSS Feeds",
            ),
        ]

    @property
    def required_packages(self) -> List[JarvisPackage]:
        return [JarvisPackage("feedparser")]

    def generate_prompt_examples(self) -> List[CommandExample]:
        return [
            CommandExample(
                voice_command="What's in the news?",
                expected_parameters={},
                is_primary=True,
            ),
            CommandExample(
                voice_command="Give me tech headlines",
                expected_parameters={"category": "tech"},
            ),
            CommandExample(
                voice_command="Any sports news?",
                expected_parameters={"category": "sports"},
            ),
            CommandExample(
                voice_command="Top 3 headlines",
                expected_parameters={"count": 3},
            ),
        ]

    def generate_adapter_examples(self) -> List[CommandExample]:
        return self.generate_prompt_examples()

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    def run(self, request_info: RequestInformation, **kwargs: Any) -> CommandResponse:
        category: str = kwargs.get("category", "general")
        count: int = kwargs.get("count", 5)

        feed_urls = list(_DEFAULT_FEEDS.get(category, _DEFAULT_FEEDS["general"]))

        # Merge custom feeds from secret
        custom = get_secret_value("NEWS_RSS_FEEDS", "integration")
        if custom:
            for url in custom.split(","):
                url = url.strip()
                if url:
                    feed_urls.append(url)

        articles = self._fetch_articles(feed_urls)

        if not articles:
            return CommandResponse.error_response(
                error_details="No news sources available. All feeds failed or returned no articles.",
            )

        articles = articles[:count]

        return CommandResponse.success_response(
            context_data={
                "category": category,
                "count": len(articles),
                "articles": articles,
            },
            wait_for_input=False,
        )

    # ------------------------------------------------------------------
    # Feed fetching
    # ------------------------------------------------------------------

    def _fetch_articles(self, feed_urls: List[str]) -> List[Dict[str, Any]]:
        """Fetch and merge articles from multiple RSS feeds."""
        all_articles: List[Dict[str, Any]] = []
        seen_titles: set[str] = set()

        for url in feed_urls:
            try:
                parsed = feedparser.parse(url)
                source = getattr(parsed.feed, "title", url)

                for entry in parsed.entries:
                    title = getattr(entry, "title", None)
                    if not title:
                        continue

                    # Deduplicate by exact title
                    title_key = title.strip().lower()
                    if title_key in seen_titles:
                        continue
                    seen_titles.add(title_key)

                    published_parsed = getattr(entry, "published_parsed", None)
                    published_ts = calendar.timegm(published_parsed) if published_parsed else 0

                    all_articles.append({
                        "title": title,
                        "summary": getattr(entry, "summary", ""),
                        "source": source,
                        "published": published_parsed,
                        "_sort_ts": published_ts,
                    })

            except Exception as e:
                logger.warning("Failed to fetch RSS feed", url=url, error=str(e))

        # Sort newest first
        all_articles.sort(key=lambda a: a["_sort_ts"], reverse=True)

        # Format published date and remove sort key
        for article in all_articles:
            pp = article.pop("_sort_ts")
            pub = article.pop("published", None)
            if pub:
                try:
                    from datetime import datetime
                    article["published"] = datetime(*pub[:6]).strftime("%Y-%m-%d")
                except (TypeError, ValueError):
                    article["published"] = None
            else:
                article["published"] = None

        return all_articles
