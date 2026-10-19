"""Shared pytest hooks for the jarvis-node-setup test suite.

The `qa_case` marker maps a test to a QA-plan case ID (e.g. CASE-001).
This hook copies the marker value into `item.user_properties`, which pytest
serializes into `<property name="qa_case" value="..."/>` inside the JUnit XML
report. `tools/parse_junit.py` then joins on those properties to produce the
case-status map posted on the PR.
"""

from __future__ import annotations

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        for marker in item.iter_markers(name="qa_case"):
            case_id = marker.args[0] if marker.args else None
            if case_id:
                item.user_properties.append(("qa_case", case_id))


@pytest.fixture
def node_command_backends():
    """Register the node's timer/audio/alert-queue/CC/LLM SDK backends, as
    scripts/main.py does at startup, and unregister them afterwards."""
    from jarvis_command_sdk.alert_queue import set_alert_queue_backend
    from jarvis_command_sdk.audio_control import set_audio_control_backend
    from jarvis_command_sdk.cc_http import set_cc_backend
    from jarvis_command_sdk.llm import set_llm_backend
    from jarvis_command_sdk.timer import set_timer_backend

    from services.command_backends import init_command_backends

    assert init_command_backends() == []
    yield
    for setter in (set_timer_backend, set_audio_control_backend,
                   set_alert_queue_backend, set_cc_backend, set_llm_backend):
        setter(None)
