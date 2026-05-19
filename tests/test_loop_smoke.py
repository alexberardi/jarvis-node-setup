"""Smoke tests for the integration-runner loop.

Verifies that the QA execution-layer plumbing works end-to-end:
- `qa_case` markers reach the JUnit XML via the user-properties hook
- The fake LLM and Whisper backends respond to canned inputs
- pytest exit status flows back through the runner workflow

These tests deliberately hit only the fakes — no Jarvis services required —
so the runner workflow can prove the loop without standing up a stack. They
live here (and not under `tests/integration/`) on purpose: that subtree's
`conftest.py` imports the production codebase (which depends on
`jarvis_command_sdk`), and pulling that in just to run a smoke test would
make the runner workflow brittle and slow.
"""

from __future__ import annotations

import os

import httpx
import pytest

FAKE_LLM_URL = os.environ.get("FAKE_LLM_URL", "http://127.0.0.1:7705")
FAKE_WHISPER_URL = os.environ.get("FAKE_WHISPER_URL", "http://127.0.0.1:7706")


@pytest.mark.qa_case("CASE-001")
def test_fake_llm_returns_canned_completion():
    """Fake LLM emits OpenAI-shaped response for the canned 'plus' prompt.

    The fake mirrors jarvis-llm-proxy-api's /v1/chat/completions wire
    format (see services/response_helpers.py:create_openai_response).
    """
    response = httpx.post(
        f"{FAKE_LLM_URL}/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "What's 25 plus 37?"}],
            "model": "fake-llm",
        },
        timeout=5.0,
    )
    response.raise_for_status()
    body = response.json()
    choice = body["choices"][0]
    assert choice["message"]["content"] == "The result is 62."
    assert choice["finish_reason"] == "stop"


@pytest.mark.qa_case("CASE-002")
def test_fake_llm_emits_tool_call_for_timer_prompt():
    """Fake emits the tool_calls payload as a JSON string in content
    (matching what LoRA-adapter-trained models do in prod and what CC's
    text-based parser expects — see tool_call_parser.parse_response)."""
    import json

    response = httpx.post(
        f"{FAKE_LLM_URL}/v1/chat/completions",
        json={
            "messages": [{"role": "user", "content": "Set a 5 minute timer"}],
            "model": "fake-llm",
        },
        timeout=5.0,
    )
    response.raise_for_status()
    body = response.json()
    choice = body["choices"][0]
    # finish_reason is "stop" because CC parses tool_calls out of the
    # content JSON itself; native finish_reason isn't the trigger.
    assert choice["finish_reason"] == "stop"
    parsed_content = json.loads(choice["message"]["content"])
    assert parsed_content["tool_calls"][0]["name"] == "set_timer"
    assert parsed_content["tool_calls"][0]["arguments"]["duration_seconds"] == 300


@pytest.mark.qa_case("CASE-003")
def test_fake_whisper_returns_canned_transcript_for_known_filename():
    # Field name `file` matches real jarvis-whisper-api (app/main.py:127).
    # CC's WhisperClient.transcribe() also sends as `file`, so CASE-208
    # uses the same shape end-to-end through CC's media proxy.
    files = {"file": ("timer_test.wav", b"\x00" * 16, "audio/wav")}
    response = httpx.post(
        f"{FAKE_WHISPER_URL}/transcribe",
        files=files,
        timeout=5.0,
    )
    response.raise_for_status()
    body = response.json()
    assert body["text"] == "Set a five minute timer"
    assert body["fake"] is True
