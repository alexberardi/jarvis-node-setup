"""
Unit tests for services.agent_context_facts: flattening a custom agent's
get_context_data() into short memory facts for jarvisd's /memories/inject.
"""

from services.agent_context_facts import (
    MAX_FACTS_PER_AGENT,
    build_memory_items,
    flatten_context,
    ttl_hours_for_interval,
)


NESTED = {
    "location": "Boston",
    "today": {"high": 72, "low": 55, "conditions": "sunny"},
    "alerts": [],
    "forecast": [
        {"day": "Tue", "high": 70},
        {"day": "Wed", "high": None},
    ],
    "tags": ["outdoor", "dry"],
    "last_error": None,
}


class TestFlattenContext:
    def test_nested_dicts_and_lists_become_path_lines(self):
        facts = flatten_context("weather", NESTED)

        assert facts == [
            ("forecast[0].day", "weather: forecast: 1: day: Tue"),
            ("forecast[0].high", "weather: forecast: 1: high: 70"),
            ("forecast[1].day", "weather: forecast: 2: day: Wed"),
            ("location", "weather: location: Boston"),
            ("tags", "weather: tags: outdoor, dry"),
            ("today.conditions", "weather: today: conditions: sunny"),
            ("today.high", "weather: today: high: 72"),
            ("today.low", "weather: today: low: 55"),
        ]

    def test_skips_empty_values_and_error_bookkeeping(self):
        facts = flatten_context(
            "a",
            {"x": "", "y": None, "z": {}, "last_error": "boom", "error_time": "t", "ok": False},
        )
        assert facts == [("ok", "a: ok: False")]

    def test_empty_or_non_dict_context_gives_no_facts(self):
        assert flatten_context("a", {}) == []
        assert flatten_context("a", None) == []  # type: ignore[arg-type]
        assert flatten_context("a", ["x"]) == []  # type: ignore[arg-type]

    def test_long_values_are_truncated(self):
        facts = flatten_context("a", {"note": "x" * 1000})
        assert len(facts) == 1
        text = facts[0][1]
        assert text.startswith("a: note: xxx")
        assert text.endswith("…")
        assert len(text) <= 400

    def test_fact_count_is_capped(self):
        data = {f"k{i:03d}": i for i in range(500)}
        facts = flatten_context("a", data)
        assert len(facts) == MAX_FACTS_PER_AGENT
        assert MAX_FACTS_PER_AGENT < 100  # one /memories/inject request

    def test_long_lists_are_capped(self):
        data = {"items": [{"n": i} for i in range(50)]}
        facts = flatten_context("a", data)
        assert len(facts) == 10
        assert facts[-1] == ("items[9].n", "a: items: 10: n: 9")

    def test_order_is_deterministic(self):
        a = flatten_context("a", {"b": 1, "a": {"y": 2, "x": 3}})
        b = flatten_context("a", {"a": {"x": 3, "y": 2}, "b": 1})
        assert a == b


class TestTtl:
    def test_ttl_is_twice_the_interval_in_hours(self):
        assert ttl_hours_for_interval(3600) == 2.0
        assert ttl_hours_for_interval(6 * 3600) == 12.0

    def test_ttl_has_a_floor_of_one_hour(self):
        assert ttl_hours_for_interval(60) == 1.0
        assert ttl_hours_for_interval(0) == 1.0

    def test_ttl_is_clamped_to_server_max(self):
        assert ttl_hours_for_interval(30 * 24 * 3600) == 720.0


class TestBuildMemoryItems:
    def test_items_carry_stable_keys_category_source_and_ttl(self):
        items = build_memory_items("weather", {"today": {"high": 72}}, 1800)

        assert items == [
            {
                "content": "weather: today: high: 72",
                "key": "agent:weather:today.high",
                "category": "agent_context",
                "source": "agent:weather",
                "ttl_hours": 1.0,
            }
        ]

    def test_keys_are_stable_across_value_changes(self):
        first = build_memory_items("w", {"today": {"high": 72}}, 3600)
        second = build_memory_items("w", {"today": {"high": 75}}, 3600)
        assert first[0]["key"] == second[0]["key"]
        assert first[0]["content"] != second[0]["content"]

    def test_fields_fit_server_limits(self):
        long_key = "k" * 800
        items = build_memory_items("n" * 200, {long_key: "v" * 5000}, 3600)
        assert len(items) == 1
        item = items[0]
        assert len(item["key"]) <= 500
        assert len(item["source"]) <= 100
        assert len(item["content"]) <= 2000
        # Long keys still stay stable run to run.
        again = build_memory_items("n" * 200, {long_key: "other"}, 3600)
        assert again[0]["key"] == item["key"]

    def test_empty_context_gives_no_items(self):
        assert build_memory_items("w", {}, 3600) == []
