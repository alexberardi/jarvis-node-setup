"""Unit tests for AnswerQuestionCommand pre-route.

Pre-route narrowly claims unambiguous knowledge-question openers — anything
that could legitimately route to time/weather/measurement/devices stays on
the LLM path.
"""

import pytest

from commands.answer_question_command import AnswerQuestionCommand


@pytest.fixture
def cmd():
    return AnswerQuestionCommand()


class TestPreRouteKnowledge:
    @pytest.mark.parametrize("phrase", [
        "tell me about Albert Einstein",
        "tell me about the French Revolution",
        "explain photosynthesis",
        "explain the theory of relativity",
        "define entropy",
        "meaning of ephemeral",
        "who is Einstein",
        "who was the first president",
        "who were the founding fathers",
        "when was the Declaration of Independence signed",
        "when did World War II end",
        "what does DNA stand for",
    ])
    def test_matches(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"query": phrase}


class TestPreRouteNoMatch:
    """Phrasings that overlap with other commands MUST fall through."""

    @pytest.mark.parametrize("phrase", [
        # Time
        "what is the time",
        "what time is it",
        "current time",
        # Weather
        "what is the weather",
        "what's the weather today",
        # Measurement conversion
        "how many ounces in a pound",
        "how many feet in a mile",
        # Devices / HA
        "where is the kitchen light",
        # Math
        "what is 5 plus 3",
        # Unrelated
        "tell me a joke",
        "tell me a story",
        "set a timer",
        "",
    ])
    def test_falls_through(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None


class TestPreRouteDisabled:
    def test_disabling_skips(self, cmd):
        result = cmd.pre_route(
            "tell me about Einstein",
            disabled_pattern_ids={"answer_question.knowledge_prefix"},
        )
        assert result is None


class TestFastPathPatterns:
    def test_pattern_id_stable(self, cmd):
        ids = {p.id for p in cmd.fast_path_patterns}
        assert ids == {"answer_question.knowledge_prefix"}
