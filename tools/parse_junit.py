#!/usr/bin/env python3
"""Parse pytest --junit-xml output into a QA-case → status map.

Coding-agent decorates tests with `@pytest.mark.qa_case("CASE-001")`. A
hook in `tests/conftest.py` copies that marker into the test's
`user_properties`, which pytest serializes into the JUnit XML as
`<property name="qa_case" value="CASE-001"/>`. This script reads that
XML, groups by qa_case, and emits a JSON map the integration-runner
workflow posts back to the originating PR.

Cases listed in `--plan-cases` but not found in the XML get status
"not-implemented" so the QA agent can call out missing coverage.

Usage:

    python tools/parse_junit.py results.xml \\
        --plan-cases CASE-001,CASE-002,CASE-003 \\
        --run-url https://github.com/owner/repo/actions/runs/123 \\
        --output results.json
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def _excerpt(text: str | None, limit: int = 240) -> str:
    if not text:
        return ""
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _status_and_detail(testcase: ET.Element) -> tuple[str, str]:
    failure = testcase.find("failure")
    if failure is not None:
        return "fail", failure.text or failure.get("message", "") or ""
    error = testcase.find("error")
    if error is not None:
        return "fail", error.text or error.get("message", "") or ""
    skipped = testcase.find("skipped")
    if skipped is not None:
        return "skipped", skipped.text or skipped.get("message", "") or ""
    return "pass", ""


def parse(xml_path: Path) -> dict[str, dict]:
    root = ET.parse(xml_path).getroot()
    cases: dict[str, dict] = {}
    for testcase in root.iter("testcase"):
        qa_case = None
        for prop in testcase.iter("property"):
            if prop.get("name") == "qa_case":
                qa_case = prop.get("value")
                break
        if not qa_case:
            continue
        status, detail = _status_and_detail(testcase)
        classname = testcase.get("classname", "")
        name = testcase.get("name", "")
        full_name = f"{classname}::{name}" if classname else name
        cases[qa_case] = {
            "status": status,
            "test_name": full_name,
            "failure_excerpt": _excerpt(detail),
        }
    return cases


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("xml", type=Path)
    parser.add_argument(
        "--plan-cases",
        default="",
        help="Comma-separated CASE-IDs from the QA plan. Cases listed here "
        "but not found in the XML are reported as 'not-implemented'.",
    )
    parser.add_argument("--output", type=Path)
    parser.add_argument("--run-url", default="")
    args = parser.parse_args()

    if not args.xml.exists():
        print(f"error: JUnit XML not found: {args.xml}", file=sys.stderr)
        sys.exit(1)

    cases = parse(args.xml)

    if args.plan_cases:
        for case_id in (c.strip() for c in args.plan_cases.split(",")):
            if case_id and case_id not in cases:
                cases[case_id] = {
                    "status": "not-implemented",
                    "test_name": "",
                    "failure_excerpt": (
                        "No test found with this qa_case marker."
                    ),
                }

    payload = {
        "run_url": args.run_url,
        "cases": cases,
        "summary": {
            "total": len(cases),
            "pass": sum(1 for c in cases.values() if c["status"] == "pass"),
            "fail": sum(1 for c in cases.values() if c["status"] == "fail"),
            "skipped": sum(
                1 for c in cases.values() if c["status"] == "skipped"
            ),
            "not_implemented": sum(
                1
                for c in cases.values()
                if c["status"] == "not-implemented"
            ),
        },
    }

    out = json.dumps(payload, indent=2)
    if args.output:
        args.output.write_text(out)
    else:
        print(out)


if __name__ == "__main__":
    main()
