"""Tests for NewsCommand — RSS news headlines."""

import time
from unittest.mock import MagicMock, patch

import pytest

from core.command_response import CommandResponse
from core.request_information import RequestInformation


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request_info() -> RequestInformation:
    return RequestInformation(
        voice_command="what's in the news",
        conversation_id="test-conv-123",
        is_validation_response=False,
    )


def _make_feed_entry(
    title: str,
    summary: str = "Summary text.",
    source: str = "Test Feed",
    published_parsed: time.struct_time | None = None,
) -> dict:
    """Create a mock feedparser entry."""
    return {
        "title": title,
        "summary": summary,
        "link": f"https://example.com/{title.lower().replace(' ', '-')}",
        "published_parsed": published_parsed or time.gmtime(),
    }


def _make_parsed_feed(entries: list[dict], feed_title: str = "Test Feed") -> MagicMock:
    """Create a mock feedparser.parse() result."""
    feed = MagicMock()
    feed.entries = [MagicMock(**e) for e in entries]
    feed.feed = MagicMock()
    feed.feed.title = feed_title
    feed.bozo = False
    return feed


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def news_cmd():
    from commands.news_command import NewsCommand
    return NewsCommand()


# ===================================================================
# Command metadata tests
# ===================================================================

class TestCommandMetadata:
    def test_command_name(self, news_cmd):
        assert news_cmd.command_name == "get_news"

    def test_keywords(self, news_cmd):
        assert "news" in news_cmd.keywords
        assert "headlines" in news_cmd.keywords

    def test_parameters(self, news_cmd):
        param_names = [p.name for p in news_cmd.parameters]
        assert "category" in param_names
        assert "count" in param_names

    def test_category_enum_values(self, news_cmd):
        category_param = next(p for p in news_cmd.parameters if p.name == "category")
        assert "general" in category_param.enum_values
        assert "tech" in category_param.enum_values
        assert "sports" in category_param.enum_values

    def test_required_packages(self, news_cmd):
        pkg_names = [p.name for p in news_cmd.required_packages]
        assert "feedparser" in pkg_names

    def test_no_required_secrets(self, news_cmd):
        # NEWS_RSS_FEEDS is optional (required=False)
        required = [s for s in news_cmd.required_secrets if s.required]
        assert len(required) == 0

    def test_has_prompt_examples(self, news_cmd):
        examples = news_cmd.generate_prompt_examples()
        assert len(examples) > 0


# ===================================================================
# News fetching tests
# ===================================================================

class TestNewsFetching:
    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_returns_articles(self, mock_secret, mock_fp, news_cmd):
        entries = [
            _make_feed_entry("Breaking Story"),
            _make_feed_entry("Another Story"),
        ]
        mock_fp.parse.return_value = _make_parsed_feed(entries)

        result = news_cmd.run(_make_request_info(), category="general", count=5)

        assert result.success is True
        articles = result.context_data["articles"]
        assert len(articles) == 2
        assert articles[0]["title"] == "Breaking Story"

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_count_limits_results(self, mock_secret, mock_fp, news_cmd):
        entries = [_make_feed_entry(f"Story {i}") for i in range(10)]
        mock_fp.parse.return_value = _make_parsed_feed(entries)

        result = news_cmd.run(_make_request_info(), category="general", count=3)

        assert result.success is True
        assert len(result.context_data["articles"]) == 3

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_sorts_by_published_descending(self, mock_secret, mock_fp, news_cmd):
        old_time = time.strptime("2026-03-10 08:00:00", "%Y-%m-%d %H:%M:%S")
        new_time = time.strptime("2026-03-12 08:00:00", "%Y-%m-%d %H:%M:%S")

        entries = [
            _make_feed_entry("Old Story", published_parsed=old_time),
            _make_feed_entry("New Story", published_parsed=new_time),
        ]
        mock_fp.parse.return_value = _make_parsed_feed(entries)

        result = news_cmd.run(_make_request_info(), category="general", count=5)

        articles = result.context_data["articles"]
        assert articles[0]["title"] == "New Story"
        assert articles[1]["title"] == "Old Story"

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_deduplicates_by_title(self, mock_secret, mock_fp, news_cmd):
        entries = [
            _make_feed_entry("Breaking: Big Event Happens"),
            _make_feed_entry("Breaking: Big Event Happens"),
        ]
        mock_fp.parse.return_value = _make_parsed_feed(entries)

        result = news_cmd.run(_make_request_info(), category="general", count=5)

        articles = result.context_data["articles"]
        assert len(articles) == 1


# ===================================================================
# Category filtering tests
# ===================================================================

class TestCategoryFiltering:
    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_tech_category(self, mock_secret, mock_fp, news_cmd):
        mock_fp.parse.return_value = _make_parsed_feed([_make_feed_entry("Tech News")])

        news_cmd.run(_make_request_info(), category="tech", count=5)

        # Should have called parse with tech feed URLs
        call_urls = [call[0][0] for call in mock_fp.parse.call_args_list]
        assert any("arstechnica" in url or "Technology" in url for url in call_urls)

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_sports_category(self, mock_secret, mock_fp, news_cmd):
        mock_fp.parse.return_value = _make_parsed_feed([_make_feed_entry("Sports News")])

        news_cmd.run(_make_request_info(), category="sports", count=5)

        call_urls = [call[0][0] for call in mock_fp.parse.call_args_list]
        assert any("sports" in url.lower() or "ncb" in url.lower() for url in call_urls)

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_defaults_to_general(self, mock_secret, mock_fp, news_cmd):
        mock_fp.parse.return_value = _make_parsed_feed([_make_feed_entry("General News")])

        news_cmd.run(_make_request_info(), count=5)

        call_urls = [call[0][0] for call in mock_fp.parse.call_args_list]
        assert any("apnews" in url or "bbc" in url for url in call_urls)


# ===================================================================
# Custom feeds tests
# ===================================================================

class TestCustomFeeds:
    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value")
    def test_custom_feeds_merged(self, mock_secret, mock_fp, news_cmd):
        mock_secret.return_value = "https://custom.example.com/rss"
        mock_fp.parse.return_value = _make_parsed_feed([_make_feed_entry("Custom Story")])

        news_cmd.run(_make_request_info(), category="general", count=5)

        call_urls = [call[0][0] for call in mock_fp.parse.call_args_list]
        assert "https://custom.example.com/rss" in call_urls

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value")
    def test_multiple_custom_feeds(self, mock_secret, mock_fp, news_cmd):
        mock_secret.return_value = "https://feed1.example.com/rss,https://feed2.example.com/rss"
        mock_fp.parse.return_value = _make_parsed_feed([_make_feed_entry("Story")])

        news_cmd.run(_make_request_info(), category="general", count=5)

        call_urls = [call[0][0] for call in mock_fp.parse.call_args_list]
        assert "https://feed1.example.com/rss" in call_urls
        assert "https://feed2.example.com/rss" in call_urls


# ===================================================================
# Error handling tests
# ===================================================================

class TestErrorHandling:
    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_all_feeds_fail(self, mock_secret, mock_fp, news_cmd):
        mock_fp.parse.side_effect = Exception("Network error")

        result = news_cmd.run(_make_request_info(), category="general", count=5)

        assert result.success is False
        assert "No news sources available" in result.error_details

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_one_feed_fails_others_succeed(self, mock_secret, mock_fp, news_cmd):
        good_feed = _make_parsed_feed([_make_feed_entry("Good Story")])

        def side_effect(url):
            if "apnews" in url:
                raise Exception("Network error")
            return good_feed

        mock_fp.parse.side_effect = side_effect

        result = news_cmd.run(_make_request_info(), category="general", count=5)

        assert result.success is True
        assert len(result.context_data["articles"]) >= 1

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_empty_feed(self, mock_secret, mock_fp, news_cmd):
        mock_fp.parse.return_value = _make_parsed_feed([])

        result = news_cmd.run(_make_request_info(), category="general", count=5)

        assert result.success is False

    @patch("commands.news_command.feedparser")
    @patch("commands.news_command.get_secret_value", return_value=None)
    def test_entry_missing_fields_handled(self, mock_secret, mock_fp, news_cmd):
        """Entries missing title or published_parsed are skipped gracefully."""
        entry_no_title = {"title": None, "summary": "No title", "link": "http://x", "published_parsed": time.gmtime()}
        entry_good = _make_feed_entry("Good Story")

        feed = MagicMock()
        entry1 = MagicMock(**entry_no_title)
        entry1.title = None
        entry2 = MagicMock(**entry_good)
        feed.entries = [entry1, entry2]
        feed.feed = MagicMock()
        feed.feed.title = "Test"
        feed.bozo = False
        mock_fp.parse.return_value = feed

        result = news_cmd.run(_make_request_info(), category="general", count=5)

        assert result.success is True
        articles = result.context_data["articles"]
        assert len(articles) == 1
        assert articles[0]["title"] == "Good Story"


# ===================================================================
# Prompt examples tests
# ===================================================================

class TestPromptExamples:
    def test_examples_have_parameters(self, news_cmd):
        for example in news_cmd.generate_prompt_examples():
            assert isinstance(example.expected_parameters, dict)

    def test_primary_example_exists(self, news_cmd):
        examples = news_cmd.generate_prompt_examples()
        primaries = [e for e in examples if e.is_primary]
        assert len(primaries) == 1
