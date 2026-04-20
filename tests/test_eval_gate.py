"""Unit tests for eval_gate summarize + delta helpers (Phase 7.2b)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import eval_gate  # type: ignore


# --- summarize_run ---


def test_summarize_run_extracts_overall_and_per_command():
    results = {
        "summary": {
            "total_tests": 63,
            "passed": 51,
            "success_rate": 81.0,
            "avg_response_time": 4.18,
        },
        "analysis": {
            "command_success_rates": {
                "calculate": {"success_rate": 100.0, "passed": 8, "total": 8},
                "music": {"success_rate": 75.0, "passed": 24, "total": 32},
            },
        },
    }
    s = eval_gate.summarize_run(results)
    assert s["pass_rate"] == 81.0
    assert s["passed"] == 51
    assert s["total"] == 63
    assert s["avg_response_time_s"] == 4.18
    assert s["per_command"]["calculate"] == {
        "success_rate": 100.0, "passed": 8, "total": 8,
    }
    assert s["per_command"]["music"]["success_rate"] == 75.0


def test_summarize_run_tolerates_missing_fields():
    s = eval_gate.summarize_run({})
    assert s["pass_rate"] == 0.0
    assert s["passed"] == 0
    assert s["total"] == 0
    assert s["avg_response_time_s"] == 0.0
    assert s["per_command"] == {}


def test_summarize_run_tolerates_missing_analysis_but_has_summary():
    s = eval_gate.summarize_run({"summary": {"success_rate": 73.0, "total_tests": 100}})
    assert s["pass_rate"] == 73.0
    assert s["per_command"] == {}


# --- build_per_command_delta ---


def test_delta_computes_basic_gains_and_regressions():
    before = {
        "calculate": {"success_rate": 87.5, "passed": 7, "total": 8},
        "music": {"success_rate": 71.9, "passed": 23, "total": 32},
    }
    after = {
        "calculate": {"success_rate": 100.0, "passed": 8, "total": 8},
        "music": {"success_rate": 75.0, "passed": 24, "total": 32},
    }
    out = eval_gate.build_per_command_delta(before, after)
    assert out["calculate"]["delta_pp"] == 12.5
    assert out["calculate"]["before"]["passed"] == 7
    assert out["calculate"]["after"]["passed"] == 8
    assert out["music"]["delta_pp"] == 3.1


def test_delta_unions_before_only_and_after_only_commands():
    """Commands that only appear on one side still show up with 0 on the missing side."""
    before = {"a": {"success_rate": 50.0, "passed": 1, "total": 2}}
    after = {"b": {"success_rate": 75.0, "passed": 3, "total": 4}}
    out = eval_gate.build_per_command_delta(before, after)
    assert set(out.keys()) == {"a", "b"}
    assert out["a"]["after"]["total"] == 0
    assert out["a"]["delta_pp"] == -50.0
    assert out["b"]["before"]["total"] == 0
    assert out["b"]["delta_pp"] == 75.0


def test_delta_rounds_to_two_decimals():
    before = {"x": {"success_rate": 33.333, "passed": 1, "total": 3}}
    after = {"x": {"success_rate": 66.667, "passed": 2, "total": 3}}
    out = eval_gate.build_per_command_delta(before, after)
    # 66.667 - 33.333 = 33.334 → rounds to 33.33
    assert out["x"]["delta_pp"] == 33.33


# --- integration: main() with --with-baseline, mocked subprocess ---


def test_main_with_baseline_emits_before_after_trailer(monkeypatch, tmp_path, capsys):
    """Mock run_test_suite so we don't actually exec the whole test suite.

    Baseline run writes a results file with 78.8% / 4 tools; adapter run
    writes 86.4%. We then read stdout for the trailer and assert the
    Phase 7.2b fields are present.
    """
    baseline_file = tmp_path / "results.baseline.json"
    adapter_file = tmp_path / "results.json"

    baseline_payload = {
        "summary": {
            "total_tests": 118,
            "passed": 93,
            "success_rate": 78.8,
            "avg_response_time": 5.88,
        },
        "analysis": {
            "command_success_rates": {
                "calculate": {"success_rate": 87.5, "passed": 7, "total": 8},
                "set_timer": {"success_rate": 86.7, "passed": 13, "total": 15},
            },
        },
    }
    adapter_payload = {
        "summary": {
            "total_tests": 118,
            "passed": 102,
            "success_rate": 86.4,
            "avg_response_time": 4.18,
        },
        "analysis": {
            "command_success_rates": {
                "calculate": {"success_rate": 100.0, "passed": 8, "total": 8},
                "set_timer": {"success_rate": 93.3, "passed": 14, "total": 15},
            },
        },
    }

    def fake_run(adapter_hash, adapter_scale, test_indices, commands, output_path):
        # First call writes baseline (adapter_hash is None); second writes adapter run.
        payload = adapter_payload if adapter_hash else baseline_payload
        output_path.write_text(json.dumps(payload))
        return 0

    monkeypatch.setattr(eval_gate, "run_test_suite", fake_run)
    monkeypatch.setattr(
        sys, "argv",
        [
            "eval_gate.py",
            "--adapter-hash", "abc123",
            "--output", str(adapter_file),
            "--baseline-output", str(baseline_file),
            "--with-baseline",
            "--tolerance", "2.0",
        ],
    )

    rc = eval_gate.main()
    assert rc == 0  # 86.4 beats 78.8 baseline

    captured = capsys.readouterr().out.splitlines()
    trailer_line = next(line for line in reversed(captured) if line.startswith("{"))
    trailer = json.loads(trailer_line)

    assert trailer["verdict"] == "PASS"
    assert trailer["pass_rate_before"] == 78.8
    assert trailer["pass_rate_after"] == 86.4
    assert trailer["latency_before_s"] == 5.88
    assert trailer["latency_after_s"] == 4.18
    assert trailer["threshold_source"] == "--with-baseline"
    assert "per_command_delta" in trailer
    assert trailer["per_command_delta"]["calculate"]["delta_pp"] == 12.5
    assert trailer["per_command_delta"]["set_timer"]["delta_pp"] == 6.6


def test_main_without_baseline_still_emits_after_fields(monkeypatch, tmp_path, capsys):
    """Legacy single-pass invocation still produces the post-run trailer shape."""
    adapter_file = tmp_path / "results.json"
    adapter_payload = {
        "summary": {
            "total_tests": 10,
            "passed": 9,
            "success_rate": 90.0,
            "avg_response_time": 3.5,
        },
        "analysis": {
            "command_success_rates": {
                "calculate": {"success_rate": 100.0, "passed": 8, "total": 8},
            },
        },
    }

    def fake_run(adapter_hash, adapter_scale, test_indices, commands, output_path):
        output_path.write_text(json.dumps(adapter_payload))
        return 0

    monkeypatch.setattr(eval_gate, "run_test_suite", fake_run)
    monkeypatch.setattr(
        sys, "argv",
        [
            "eval_gate.py",
            "--adapter-hash", "abc",
            "--output", str(adapter_file),
            "--threshold", "85.0",
        ],
    )
    rc = eval_gate.main()
    assert rc == 0

    trailer = json.loads(capsys.readouterr().out.splitlines()[-1])
    # No --with-baseline → no before/latency_before/per_command_delta.
    assert trailer["pass_rate_after"] == 90.0
    assert trailer["latency_after_s"] == 3.5
    assert "pass_rate_before" not in trailer
    assert "latency_before_s" not in trailer
    assert "per_command_delta" not in trailer
    assert trailer["per_command_after"]["calculate"]["success_rate"] == 100.0
