"""Unit tests for TodoListCommand pre-route.

All six actions are deterministic when the utterance names the to-do/task
list explicitly. Fuzzy shapes ("I need to call the dentist") fall through
to the LLM, as do reminder phrasings ("remind me to call mom").
"""

import pytest

from commands.todo_list_command import TodoListCommand


@pytest.fixture
def cmd():
    return TodoListCommand()


class TestPreRouteAdd:
    @pytest.mark.parametrize("phrase,expected", [
        ("add call the dentist to the to-do list", ["call the dentist"]),
        ("add fold the laundry to my to do list", ["fold the laundry"]),
        ("put water the plants on the task list", ["water the plants"]),
        ("Add take out the trash to our todo list.", ["take out the trash"]),
        # A single task containing "and" is NOT split by the fast path.
        ("add call mom and dad to my to-do list", ["call mom and dad"]),
    ])
    def test_add(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "add", "tasks": expected}


class TestPreRouteRead:
    @pytest.mark.parametrize("phrase", [
        "what's on the to-do list",
        "what is on my todo list?",
        "read me the to-do list",
        "show me the task list",
        "list my to-do list",
    ])
    def test_read(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "read"}


class TestPreRouteComplete:
    @pytest.mark.parametrize("phrase,expected", [
        ("mark the laundry on my to-do list done", ["the laundry"]),
        ("mark the laundry on my to-do list as complete", ["the laundry"]),
        ("mark wash the car as done on my todo list", ["wash the car"]),
        ("mark call the dentist finished on the to-do list", ["call the dentist"]),
        ("check the laundry off my to-do list", ["the laundry"]),
        ("check off water the plants on my to-do list", ["water the plants"]),
        ("cross mow the lawn off the task list", ["mow the lawn"]),
    ])
    def test_complete(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "complete", "tasks": expected}


class TestPreRouteUncomplete:
    @pytest.mark.parametrize("phrase,expected", [
        ("mark the laundry on my to-do list not done", ["the laundry"]),
        ("mark the laundry on my to-do list as incomplete", ["the laundry"]),
        ("mark wash the car undone on my todo list", ["wash the car"]),
        ("mark call the dentist as not finished on the to-do list", ["call the dentist"]),
        ("uncheck mow the lawn on my to-do list", ["mow the lawn"]),
    ])
    def test_uncomplete(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "uncomplete", "tasks": expected}

    def test_not_done_does_not_route_to_complete(self, cmd):
        """The disjoint status words keep 'not done' out of the complete path."""
        result = cmd.pre_route("mark the laundry on my to-do list not done")
        assert result.arguments["action"] == "uncomplete"

    @pytest.mark.parametrize("phrase", [
        "mark call dad as not done on my to-do list",
        "mark the laundry as not finished on my to-do list",
        "mark the report as not complete on my to-do list",
    ])
    def test_complete_pattern_rejects_not_done_even_with_uncomplete_disabled(
        self, cmd, phrase,
    ):
        """The complete and uncomplete patterns are mutually exclusive on their
        own — disabling the uncomplete fast-path must NOT let a 'not done'
        phrasing fall into complete (which would mark it DONE, the opposite
        intent). With uncomplete disabled it falls through to the LLM, never
        to complete."""
        result = cmd.pre_route(
            phrase, disabled_pattern_ids={"todo_list.uncomplete"}
        )
        assert result is None


class TestPreRouteRemove:
    @pytest.mark.parametrize("phrase,expected", [
        ("remove fold the laundry from the to-do list", ["fold the laundry"]),
        ("take wash the car off my to-do list", ["wash the car"]),
        ("delete call the dentist from my todo list", ["call the dentist"]),
    ])
    def test_remove(self, cmd, phrase, expected):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "remove", "tasks": expected}


class TestPreRouteClear:
    @pytest.mark.parametrize("phrase", [
        "clear the to-do list",
        "empty my todo list",
        "wipe the task list",
    ])
    def test_clear(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "clear"}


class TestPreRouteFallthrough:
    """Phrases that MUST fall through to the LLM (or other commands)."""

    @pytest.mark.parametrize("phrase", [
        # Reminders — never steal these
        "remind me to call the dentist",
        "remind me to take out the trash at 8",
        # Shopping list — different noun, not ours
        "add milk to the shopping list",
        "what's on the grocery list",
        # Fuzzy shapes the LLM should interpret
        "I need to call the dentist",
        "don't let me forget to water the plants",
        # "mark X done" with no list noun → LLM (could be many things)
        "mark the laundry as done",
        # Bare export verbs are the export command's job
        "send my to-do list to my phone",
    ])
    def test_falls_through(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None
