"""Eval gate wrapper (Phase 2 + Phase 7.2 enrichment).

Runs `test_command_parsing.py` — optionally pinned to a specific LoRA adapter
via --adapter-hash — parses `test_results.json`, and exits 0/1 depending on
whether the pass rate clears the threshold.

Default verdict: "hold baseline within tolerance". The Phase 6 ablation story
means an adapter doesn't have to *beat* baseline — it just has to *hold*
accuracy while enabling prompt shrink. So the default tolerance is a 1pp
drop-off below baseline.

Threshold source, in order:
  1. --with-baseline  → run the suite WITHOUT the adapter first; use THAT as
                         the threshold (and emit a before/after trailer the
                         Phase 7 proposal flow consumes).
  2. --threshold <float>
  3. EVAL_THRESHOLD env var
  4. (TODO Phase 5) currently-deployed adapter's recorded pass rate from DB
  5. Literal 85.59% baseline from the latest pre-adapter test_command_parsing.py run

Usage:
    python eval_gate.py --adapter-hash abc123...
    python eval_gate.py --adapter-hash abc --with-baseline  # 2× cost, rich trailer
    python eval_gate.py --adapter-hash abc --threshold 82.0 --tolerance 0.5
    python eval_gate.py --command music                   # subset eval only

Exit codes:
    0 — pass rate ≥ threshold − tolerance  (and per-speaker side-condition met)
    1 — below threshold, or per-speaker regression > 2pp, or test run failed
    2 — configuration error (can't find test runner, etc.)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
TEST_RUNNER = REPO_ROOT / "test_command_parsing.py"
DEFAULT_RESULTS_PATH = REPO_ROOT / "test_results.json"

# Baseline pass-rate from the latest pre-adapter test run (documented in
# jarvis-tts/CLAUDE.md as the project-wide target). Updated as baselines shift.
BASELINE_LITERAL = 85.59


def resolve_threshold(arg_value: float | None) -> tuple[float, str]:
    """Returns (threshold_pct, source_label)."""
    if arg_value is not None:
        return arg_value, "--threshold"
    env_val = os.getenv("EVAL_THRESHOLD")
    if env_val:
        try:
            return float(env_val), "EVAL_THRESHOLD env"
        except ValueError:
            pass
    # TODO(Phase 5): look up currently-deployed adapter's recorded pass rate
    # from the active_adapter table in command-center DB. For now, fall through
    # to the literal baseline.
    return BASELINE_LITERAL, "literal baseline"


def run_test_suite(
    adapter_hash: str | None,
    adapter_scale: float,
    test_indices: list[int] | None,
    commands: list[str] | None,
    output_path: Path,
) -> int:
    """Invokes test_command_parsing.py as a subprocess. Returns its exit code."""
    if not TEST_RUNNER.is_file():
        print(f"ERROR: test runner not found at {TEST_RUNNER}", file=sys.stderr)
        return 2

    cmd: list[str] = [sys.executable, str(TEST_RUNNER), "--output", str(output_path)]
    if adapter_hash:
        cmd += ["--adapter-hash", adapter_hash, "--adapter-scale", str(adapter_scale)]
    if test_indices:
        cmd += ["--test-indices", *[str(i) for i in test_indices]]
    if commands:
        cmd += ["--command", *commands]

    print(f"▶ running: {' '.join(cmd)}", file=sys.stderr)
    proc = subprocess.run(cmd, cwd=str(REPO_ROOT))
    return proc.returncode


def load_results(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"test_results.json not found at {path}")
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def summarize_run(results: dict) -> dict:
    """Extract the 3 metrics the Phase 7 proposal cares about.

    - pass_rate: overall %
    - avg_response_time_s: mean wall time per test
    - per_command: {cmd: {success_rate, passed, total}}
    """
    summary = results.get("summary") or {}
    analysis = results.get("analysis") or {}
    per_cmd_raw = analysis.get("command_success_rates") or {}
    per_cmd: dict[str, dict[str, float | int]] = {}
    for cmd, stats in per_cmd_raw.items():
        per_cmd[cmd] = {
            "success_rate": float(stats.get("success_rate", 0.0)),
            "passed": int(stats.get("passed", 0)),
            "total": int(stats.get("total", 0)),
        }
    return {
        "pass_rate": float(summary.get("success_rate", 0.0)),
        "passed": int(summary.get("passed", 0)),
        "total": int(summary.get("total_tests", 0)),
        "avg_response_time_s": float(summary.get("avg_response_time", 0.0)),
        "per_command": per_cmd,
    }


def build_per_command_delta(
    before: dict[str, dict[str, float | int]],
    after: dict[str, dict[str, float | int]],
) -> dict[str, dict]:
    """Merge before/after per-command stats into a single delta dict.

    Keys are union of before+after commands; values include both sides plus a
    computed delta_pp. Mobile renders this directly as the wins/regressions
    list in the proposal preview.
    """
    out: dict[str, dict] = {}
    for cmd in sorted(set(before.keys()) | set(after.keys())):
        b = before.get(cmd) or {}
        a = after.get(cmd) or {}
        before_rate = float(b.get("success_rate", 0.0))
        after_rate = float(a.get("success_rate", 0.0))
        out[cmd] = {
            "before": {
                "success_rate": before_rate,
                "passed": int(b.get("passed", 0)),
                "total": int(b.get("total", 0)),
            },
            "after": {
                "success_rate": after_rate,
                "passed": int(a.get("passed", 0)),
                "total": int(a.get("total", 0)),
            },
            "delta_pp": round(after_rate - before_rate, 2),
        }
    return out


def compute_per_speaker(results: dict) -> dict[str, dict[str, int | float]]:
    """Per-speaker pass/fail breakdown.

    Returns {} if no test cases carry speaker_user_id — the current suite
    doesn't tag tests by speaker, so this is a no-op today. The structure is
    here so that Phase-3-imported real-world test cases (which DO carry speaker)
    get automatic per-speaker reporting without further changes.
    """
    by_speaker: dict[str, dict[str, int]] = {}
    for row in results.get("test_results", []) or []:
        spk = row.get("speaker_user_id")
        if spk is None:
            continue
        key = str(spk)
        bucket = by_speaker.setdefault(key, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if row.get("passed"):
            bucket["passed"] += 1
    return {
        k: {
            "passed": v["passed"],
            "total": v["total"],
            "pct": (100.0 * v["passed"] / v["total"]) if v["total"] else 0.0,
        }
        for k, v in by_speaker.items()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 2 eval gate")
    parser.add_argument("--adapter-hash", type=str, default=None,
                        help="Server-side adapter to pin during the eval run")
    parser.add_argument("--adapter-scale", type=float, default=1.0,
                        help="LoRA scale when --adapter-hash is set")
    parser.add_argument("--threshold", type=float, default=None,
                        help="Baseline pass-rate percent to compare against. "
                             "Default: EVAL_THRESHOLD env or 85.59%% literal.")
    parser.add_argument("--tolerance", type=float, default=1.0,
                        help="How far below threshold is still 'hold' (default 1.0pp). "
                             "Supports the Phase 6 ablation story: adapter only needs "
                             "to hold accuracy, not beat baseline.")
    parser.add_argument("--per-speaker-tolerance", type=float, default=2.0,
                        help="Max regression any single ≥10-example speaker may "
                             "have relative to their baseline (default 2.0pp). "
                             "Active only when test rows carry speaker_user_id.")
    parser.add_argument("--test-indices", nargs="+", type=int, default=None,
                        help="Subset of test indices to run (passed through)")
    parser.add_argument("--command", nargs="+", type=str, default=None,
                        help="Subset of commands to run (passed through)")
    parser.add_argument("--output", type=Path, default=DEFAULT_RESULTS_PATH,
                        help="Path for the test_results.json output (adapter run)")
    parser.add_argument("--baseline-output", type=Path, default=None,
                        help="Path for the baseline results file when "
                             "--with-baseline is set (default: <output>.baseline.json)")
    parser.add_argument("--with-baseline", action="store_true",
                        help="Run the suite WITHOUT the adapter first to measure "
                             "the live baseline. Doubles cost but produces the "
                             "before/after trailer the Phase 7 proposal flow needs.")
    parser.add_argument("--skip-run", action="store_true",
                        help="Skip the test run — just evaluate an existing results file")
    args = parser.parse_args()

    threshold_pct, threshold_source = resolve_threshold(args.threshold)

    # If --with-baseline is set and we actually have an adapter to compare
    # against, run the suite once without the adapter first. That gives us a
    # fresh, apples-to-apples baseline for this exact hardware/prompt state.
    baseline_summary: dict | None = None
    baseline_output = args.baseline_output or args.output.with_suffix(".baseline.json")
    if args.with_baseline and args.adapter_hash and not args.skip_run:
        print("▶ baseline pass (no adapter)", file=sys.stderr)
        rc = run_test_suite(
            adapter_hash=None,
            adapter_scale=1.0,
            test_indices=args.test_indices,
            commands=args.command,
            output_path=baseline_output,
        )
        if rc != 0:
            print(f"FAIL: baseline test runner exited {rc}", file=sys.stderr)
            return 1
        try:
            baseline_results = load_results(baseline_output)
        except (FileNotFoundError, json.JSONDecodeError) as e:
            print(f"FAIL: could not read baseline results: {e}", file=sys.stderr)
            return 1
        baseline_summary = summarize_run(baseline_results)
        # Measured baseline overrides resolved threshold.
        threshold_pct = baseline_summary["pass_rate"]
        threshold_source = "--with-baseline"
    elif args.with_baseline and args.skip_run and args.baseline_output:
        # Allow a previously-written baseline file to be reused under --skip-run
        try:
            baseline_results = load_results(args.baseline_output)
            baseline_summary = summarize_run(baseline_results)
            threshold_pct = baseline_summary["pass_rate"]
            threshold_source = "--with-baseline (cached)"
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    floor_pct = threshold_pct - args.tolerance

    if not args.skip_run:
        print(
            "▶ adapter pass" if args.adapter_hash else "▶ single pass (no adapter)",
            file=sys.stderr,
        )
        rc = run_test_suite(
            adapter_hash=args.adapter_hash,
            adapter_scale=args.adapter_scale,
            test_indices=args.test_indices,
            commands=args.command,
            output_path=args.output,
        )
        if rc != 0:
            print(f"FAIL: test runner exited {rc}", file=sys.stderr)
            return 1

    try:
        results = load_results(args.output)
    except FileNotFoundError as e:
        print(f"FAIL: {e}", file=sys.stderr)
        return 2
    except json.JSONDecodeError as e:
        print(f"FAIL: could not parse test_results.json: {e}", file=sys.stderr)
        return 1

    after_summary = summarize_run(results)
    pass_pct = after_summary["pass_rate"]
    total = after_summary["total"]
    passed = after_summary["passed"]
    delta_pp = pass_pct - threshold_pct

    # Per-speaker side condition (no-op unless tests carry speaker_user_id)
    per_speaker = compute_per_speaker(results)
    speaker_fails: list[str] = []
    if per_speaker:
        for spk, stats in per_speaker.items():
            if stats["total"] < 10:
                continue  # only enforce for ≥10-example speakers
            # Compare against overall threshold as the speaker baseline —
            # until Phase 5 records per-speaker baselines, this is the best we have.
            s_delta = stats["pct"] - threshold_pct
            if s_delta < -args.per_speaker_tolerance:
                speaker_fails.append(
                    f"speaker {spk}: {stats['pct']:.2f}% ({stats['passed']}/{stats['total']}, "
                    f"{s_delta:+.2f}pp)"
                )

    # Verdict
    hold_ok = pass_pct >= floor_pct
    speaker_ok = not speaker_fails

    adapter_tag = f" adapter={args.adapter_hash[:12]}…" if args.adapter_hash else " (no adapter)"
    sign = "+" if delta_pp >= 0 else ""
    verdict = "PASS" if (hold_ok and speaker_ok) else "FAIL"
    print(
        f"{verdict} {pass_pct:.2f}% ({passed}/{total}) "
        f"vs baseline {threshold_pct:.2f}% ({threshold_source}) "
        f"{sign}{delta_pp:.2f}pp "
        f"tolerance={args.tolerance}pp"
        f"{adapter_tag}"
    )

    if per_speaker:
        for spk, stats in sorted(per_speaker.items()):
            marker = "⚠️" if f"speaker {spk}:" in " ".join(speaker_fails) else "  "
            print(f"  {marker} speaker {spk}: {stats['passed']}/{stats['total']} = {stats['pct']:.2f}%")

    if speaker_fails:
        print(f"FAIL: per-speaker regression >{args.per_speaker_tolerance}pp:", file=sys.stderr)
        for msg in speaker_fails:
            print(f"  - {msg}", file=sys.stderr)

    # Machine-readable JSON tail for orchestrators.
    # When --with-baseline ran, baseline_summary is populated so the trailer
    # carries before/after pass rates, latencies, and per-command deltas —
    # the Phase 7 proposal flow reads these to render the mobile preview.
    trailer: dict = {
        "verdict": verdict,
        "pass_rate": pass_pct,
        "passed": passed,
        "total": total,
        "threshold": threshold_pct,
        "threshold_source": threshold_source,
        "tolerance": args.tolerance,
        "delta_pp": delta_pp,
        "adapter_hash": args.adapter_hash,
        "per_speaker": per_speaker,
        "speaker_fails": speaker_fails,
        "pass_rate_after": pass_pct,
        "latency_after_s": after_summary["avg_response_time_s"],
        "per_command_after": after_summary["per_command"],
    }
    if baseline_summary is not None:
        trailer["pass_rate_before"] = baseline_summary["pass_rate"]
        trailer["latency_before_s"] = baseline_summary["avg_response_time_s"]
        trailer["per_command_before"] = baseline_summary["per_command"]
        trailer["per_command_delta"] = build_per_command_delta(
            baseline_summary["per_command"], after_summary["per_command"],
        )
    print(json.dumps(trailer))

    return 0 if (hold_ok and speaker_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
