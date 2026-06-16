"""Unit tests for ExportTodoListCommand pre-route.

One anchored pattern: 'send|export (my|the|our) to-do/task list (to my
phone)', plus 'show|pull up|open <list> on my phone'. A bare 'show me my
to-do list' (no phone tail) stays a READ on the manage command. Messaging,
other exports, and list edits fall through.
"""

import pytest

from commands.export_todo_list_command import ExportTodoListCommand


@pytest.fixture
def cmd():
    return ExportTodoListCommand()


class TestPreRouteExport:
    @pytest.mark.parametrize("phrase", [
        "send my to-do list to my phone",
        "send the to-do list",
        "send me my to do list",
        "export the to-do list",
        "export my task list",
        "Export our to-do list to my phone.",
        "SEND MY TODO LIST",
        "show me my to-do list on my phone",
        "pull up my to-do list on my phone",
        "open the task list on my phone",
    ])
    def test_export(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {}

    def test_disabled_pattern_falls_through(self, cmd):
        result = cmd.pre_route(
            "send my to-do list to my phone",
            disabled_pattern_ids={"export_todo_list.export"},
        )
        assert result is None


class TestPreRouteFallthrough:
    """Phrases that MUST fall through to the LLM (or other commands)."""

    @pytest.mark.parametrize("phrase", [
        # Bare read — spoken aloud by todo_list, not an export
        "show me my to-do list",
        "what's on my to-do list",
        "read me my to-do list",
        # Messaging — never steal these
        "send a text to mom",
        "send a message to dad",
        # Other exports
        "export my contacts",
        "send my shopping list to walmart",
        # List edits belong to todo_list
        "add call the dentist to my to-do list",
        "clear my to-do list",
        # Fuzzy
        "can you get my to-do list onto my phone somehow",
    ])
    def test_falls_through(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None
