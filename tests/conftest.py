"""Shared pytest hooks for the jarvis-node-setup test suite.

The `qa_case` marker maps a test to a QA-plan case ID (e.g. CASE-001).
This hook copies the marker value into `item.user_properties`, which pytest
serializes into `<property name="qa_case" value="..."/>` inside the JUnit XML
report. `tools/parse_junit.py` then joins on those properties to produce the
case-status map posted on the PR.
"""

from __future__ import annotations


def pytest_collection_modifyitems(items):
    for item in items:
        for marker in item.iter_markers(name="qa_case"):
            case_id = marker.args[0] if marker.args else None
            if case_id:
                item.user_properties.append(("qa_case", case_id))
