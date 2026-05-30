"""Unit tests for TellAJokeCommand pre-route."""

import pytest

from commands.tell_a_joke_command import TellAJokeCommand


@pytest.fixture
def cmd():
    return TellAJokeCommand()


class TestPreRouteBare:
    @pytest.mark.parametrize("phrase", [
        "tell me a joke",
        "tell me another joke",
        "make me laugh",
        "say something funny",
        "got any jokes",
        "got a joke",
        "i want a joke",
        "give me a joke",
    ])
    def test_bare(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {}


class TestPreRouteTopic:
    @pytest.mark.parametrize("phrase,topic", [
        ("tell me a joke about cats", "cats"),
        ("tell me a joke about programming", "programming"),
        ("got any jokes about animals", "animals"),
        ("make me laugh with a joke about space", "space"),
        ("i want a joke about politics", "politics"),
    ])
    def test_topic(self, cmd, phrase, topic):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"topic": topic}


class TestPreRouteStyle:
    def test_dad_joke(self, cmd):
        result = cmd.pre_route("tell me a dad joke")
        assert result is not None
        assert result.arguments == {"topic": "dad jokes"}

    def test_knock_knock(self, cmd):
        result = cmd.pre_route("give me a knock knock joke")
        assert result is not None
        assert result.arguments == {"topic": "knock knock"}

    def test_knock_dash_knock(self, cmd):
        result = cmd.pre_route("tell me a knock-knock joke")
        assert result is not None
        assert result.arguments == {"topic": "knock knock"}


class TestPreRouteNoMatch:
    @pytest.mark.parametrize("phrase", [
        "tell me about Einstein",     # answer_question
        "tell me a story",            # tell_story
        "set a timer",                # timer
        "what time is it",            # timezone
        "",
    ])
    def test_returns_none(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestFastPathPatterns:
    def test_ids_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {
            "tell_joke.bare",
            "tell_joke.topic",
            "tell_joke.style",
        }
