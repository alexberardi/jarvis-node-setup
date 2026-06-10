"""Unit tests for ShoppingListCommand pre-route.

All five actions are deterministic when the utterance names the
shopping/grocery list explicitly. Fuzzy shapes ("we're out of milk")
fall through to the LLM, as do reminder and playlist phrasings.
"""

import pytest

from commands.shopping_list_command import ShoppingListCommand


@pytest.fixture
def cmd():
    return ShoppingListCommand()


class TestPreRouteAdd:
    @pytest.mark.parametrize("phrase,expected_items", [
        ("add milk to the shopping list", ["milk"]),
        ("add milk to my shopping list", ["milk"]),
        ("put bananas on the grocery list", ["bananas"]),
        ("Add paper towels to our shopping list.", ["paper towels"]),
        ("add milk, eggs, and butter to the shopping list", ["milk", "eggs", "butter"]),
        ("put apples and oranges on the groceries list", ["apples", "oranges"]),
        ("add some milk to the grocery list", ["milk"]),
    ])
    def test_add(self, cmd, phrase, expected_items):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "add", "items": expected_items}


class TestPreRouteRead:
    @pytest.mark.parametrize("phrase", [
        "what's on the shopping list",
        "what is on my shopping list?",
        "read me the grocery list",
        "show me the shopping list",
        "list my shopping list",
    ])
    def test_read(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "read"}


class TestPreRouteCheckOff:
    @pytest.mark.parametrize("phrase,expected_items", [
        ("check milk off the shopping list", ["milk"]),
        ("check off milk on the shopping list", ["milk"]),
        ("cross bananas off the grocery list", ["bananas"]),
        ("check eggs and butter off my shopping list", ["eggs", "butter"]),
    ])
    def test_check_off(self, cmd, phrase, expected_items):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "check_off", "items": expected_items}


class TestPreRouteRemove:
    @pytest.mark.parametrize("phrase,expected_items", [
        ("remove milk from the shopping list", ["milk"]),
        ("take chicken off the grocery list", ["chicken"]),
        ("delete eggs from my shopping list", ["eggs"]),
    ])
    def test_remove(self, cmd, phrase, expected_items):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "remove", "items": expected_items}


class TestPreRouteClear:
    @pytest.mark.parametrize("phrase", [
        "clear the shopping list",
        "empty my grocery list",
        "wipe the shopping list",
    ])
    def test_clear(self, cmd, phrase):
        result = cmd.pre_route(phrase)
        assert result is not None
        assert result.arguments == {"action": "clear"}


class TestPreRouteFallthrough:
    """Phrases that MUST fall through to the LLM (or other commands)."""

    @pytest.mark.parametrize("phrase", [
        # Reminders — never steal these
        "remind me to buy milk",
        "remind me to buy milk tomorrow at 5",
        # Music — playlist, not shopping list
        "add bohemian rhapsody to my playlist",
        # Fuzzy shapes the LLM should interpret
        "we're out of milk",
        "I need to get eggs at the store",
        "do we need anything from the store",
        # Reminder-style clear (claimed by reminder.delete_all)
        "clear all my reminders",
    ])
    def test_falls_through(self, cmd, phrase):
        assert cmd.pre_route(phrase) is None
