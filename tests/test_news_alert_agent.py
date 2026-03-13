"""Tests for NewsAlertAgent."""

from unittest.mock import MagicMock, patch

import pytest

from agents.news_alert_agent import ALERT_TTL_HOURS, NewsAlertAgent


class TestNewsAlertAgent:
    def setup_method(self) -> None:
        self.agent = NewsAlertAgent()

    def test_properties(self) -> None:
        assert self.agent.name == "news_alerts"
        assert self.agent.include_in_context is False
        assert self.agent.schedule.run_on_startup is True
        assert self.agent.schedule.interval_seconds == 1800

    def test_no_required_secrets(self) -> None:
        assert self.agent.required_secrets == []

    @pytest.mark.asyncio
    @patch("commands.news_command._DEFAULT_FEEDS", {"general": ["http://fake.rss"]})
    @patch("commands.news_command.NewsCommand._fetch_articles")
    async def test_first_run_seeds_no_alerts(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = [
            {"title": "Article 1", "summary": "Summary 1"},
            {"title": "Article 2", "summary": "Summary 2"},
        ]

        await self.agent.run()
        assert self.agent.get_alerts() == []
        assert len(self.agent._previous_titles) == 2

    @pytest.mark.asyncio
    @patch("commands.news_command._DEFAULT_FEEDS", {"general": ["http://fake.rss"]})
    @patch("commands.news_command.NewsCommand._fetch_articles")
    async def test_second_run_detects_new_articles(self, mock_fetch: MagicMock) -> None:
        # First run — seed
        mock_fetch.return_value = [
            {"title": "Old Article", "summary": "Old summary"},
        ]
        await self.agent.run()

        # Second run — new article appears
        mock_fetch.return_value = [
            {"title": "Old Article", "summary": "Old summary"},
            {"title": "Breaking News", "summary": "Big story"},
        ]
        await self.agent.run()

        alerts = self.agent.get_alerts()
        assert len(alerts) == 1
        assert alerts[0].title == "Breaking News"
        assert alerts[0].priority == 1

    @pytest.mark.asyncio
    @patch("commands.news_command._DEFAULT_FEEDS", {"general": ["http://fake.rss"]})
    @patch("commands.news_command.NewsCommand._fetch_articles")
    async def test_no_new_articles_no_alerts(self, mock_fetch: MagicMock) -> None:
        articles = [{"title": "Same Article", "summary": "Same"}]
        mock_fetch.return_value = articles

        await self.agent.run()  # seed
        await self.agent.run()  # same articles

        assert self.agent.get_alerts() == []

    @pytest.mark.asyncio
    @patch("commands.news_command._DEFAULT_FEEDS", {"general": ["http://fake.rss"]})
    @patch("commands.news_command.NewsCommand._fetch_articles")
    async def test_alert_ttl(self, mock_fetch: MagicMock) -> None:
        mock_fetch.return_value = [{"title": "Old", "summary": ""}]
        await self.agent.run()

        mock_fetch.return_value = [
            {"title": "Old", "summary": ""},
            {"title": "New", "summary": ""},
        ]
        await self.agent.run()

        alerts = self.agent.get_alerts()
        assert len(alerts) == 1
        ttl = (alerts[0].expires_at - alerts[0].created_at).total_seconds() / 3600
        assert ttl == pytest.approx(ALERT_TTL_HOURS, abs=0.01)

    def test_get_context_data_empty(self) -> None:
        assert self.agent.get_context_data() == {}
