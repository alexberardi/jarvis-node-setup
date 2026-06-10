"""Unit tests for ExportShoppingListCommand pre-route.

One anchored pattern: '(send|export) (my|the|our) (shopping|grocery|
groceries) list (to walmart|to the store|to notes|to my notes)'. The
destination tail maps to a provider argument (walmart/the store →
walmart, notes/my notes → notes); no destination keeps the configured
EXPORT_PROVIDER secret as the default. Anything else — texting people,
exporting contacts, list edits — falls through to the LLM or to other
commands' fast paths.
"""

import pytest

from commands.export_shopping_list_command import ExportShoppingListCommand


@pytest.fixture
def cmd():
    return ExportShoppingListCommand()


class TestPreRouteExport:
    @pytest.mark.parametrize("phrase,expected_args", [
        # Explicit walmart destinations
        ("send my shopping list to walmart", {"provider": "walmart"}),
        ("send the shopping list to walmart", {"provider": "walmart"}),
        ("Send my groceries list to the store.", {"provider": "walmart"}),
        ("export the grocery list to walmart", {"provider": "walmart"}),
        ("EXPORT MY SHOPPING LIST TO WALMART", {"provider": "walmart"}),
        # Explicit notes destinations
        ("send my shopping list to my notes", {"provider": "notes"}),
        ("send my shopping list to notes", {"provider": "notes"}),
        ("export the grocery list to notes", {"provider": "notes"}),
        ("Send the shopping list to my notes!", {"provider": "notes"}),
        # No destination → secret default (empty args)
        ("export the shopping list", {}),
        ("export my shopping list", {}),
        ("send our grocery list", {}),
        ("send the shopping list", {}),
        ("send shopping list", {}),
    ])
    def test_export(self, cmd, phrase, expected_args):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == expected_args

    def test_disabled_pattern_falls_through(self, cmd):
        result = cmd.pre_route(
            "send my shopping list to walmart",
            disabled_pattern_ids={"export_shopping_list.export"},
        )
        assert result is None


class TestPreRouteFallthrough:
    """Phrases that MUST fall through to the LLM (or other commands)."""

    @pytest.mark.parametrize("phrase", [
        # Messaging — never steal these
        "send a text to mom",
        "send a message to dad",
        # Other exports
        "export my contacts",
        "export my calendar",
        # Unknown destination — anchored tail only knows
        # walmart/the store/notes/my notes
        "send my shopping list to amazon",
        "send my shopping list to the notes app",
        # List edits belong to shopping_list
        "add milk to the shopping list",
        "what's on the shopping list",
        "clear the shopping list",
        "check milk off the shopping list",
        # Fuzzy shapes the LLM should interpret
        "can you get my shopping list over to walmart somehow",
        "send me the shopping list",
    ])
    def test_falls_through(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None
