"""NewsAlertAgent — monitors RSS feeds and produces alerts for new articles.

Runs every 30 minutes. Compares against previous run's titles to detect new
articles. Alerts have a 4-hour TTL and low priority (1).
"""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from jarvis_log_client import JarvisLogger

from core.alert import Alert
from core.ijarvis_agent import AgentSchedule, IJarvisAgent
from core.ijarvis_secret import IJarvisSecret

logger = JarvisLogger(service="jarvis-node")

# 30 minutes
REFRESH_INTERVAL_SECONDS = 1800
ALERT_TTL_HOURS = 4


class NewsAlertAgent(IJarvisAgent):
    """Background agent that monitors RSS feeds for new headlines."""

    def __init__(self) -> None:
        self._previous_titles: set[str] = set()
        self._current_articles: List[Dict[str, Any]] = []
        self._alerts: List[Alert] = []

    @property
    def name(self) -> str:
        return "news_alerts"

    @property
    def description(self) -> str:
        return "Monitors RSS news feeds and generates alerts for new headlines"

    @property
    def schedule(self) -> AgentSchedule:
        return AgentSchedule(
            interval_seconds=REFRESH_INTERVAL_SECONDS,
            run_on_startup=True,
        )

    @property
    def required_secrets(self) -> List[IJarvisSecret]:
        return []  # Public RSS feeds, no secrets needed

    @property
    def include_in_context(self) -> bool:
        return False  # Alert-only, not injected into voice context

    async def run(self) -> None:
        """Fetch news and detect new articles since last run."""
        try:
            from commands.news_command import NewsCommand

            cmd = NewsCommand()
            # Use default feeds for general news
            from commands.news_command import _DEFAULT_FEEDS
            feed_urls = list(_DEFAULT_FEEDS.get("general", []))
            articles = cmd._fetch_articles(feed_urls)

            current_titles = {a["title"].strip().lower() for a in articles if a.get("title")}

            # On first run, just seed — don't alert on everything
            if not self._previous_titles:
                self._previous_titles = current_titles
                self._current_articles = articles
                logger.info("News agent seeded", article_count=len(articles))
                self._alerts = []
                return

            # Detect new articles
            new_titles = current_titles - self._previous_titles
            now = datetime.now(timezone.utc)
            self._alerts = []

            for article in articles:
                title = article.get("title", "")
                if title.strip().lower() in new_titles:
                    self._alerts.append(Alert(
                        source_agent=self.name,
                        title=title,
                        summary=article.get("summary", "")[:200],
                        created_at=now,
                        expires_at=now + timedelta(hours=ALERT_TTL_HOURS),
                        priority=1,
                    ))

            self._previous_titles = current_titles
            self._current_articles = articles

            if self._alerts:
                logger.info("News agent found new articles", count=len(self._alerts))

        except Exception as e:
            logger.error("News agent run failed", error=str(e))
            self._alerts = []

    def get_context_data(self) -> Dict[str, Any]:
        return {}

    def get_alerts(self) -> List[Alert]:
        return list(self._alerts)
