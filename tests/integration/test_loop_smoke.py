"""Smoke tests for the integration-runner loop.

Verifies that the QA execution-layer plumbing works end-to-end:
- `qa_case` markers reach the JUnit XML via the user-properties hook
- The fake LLM and Whisper backends respond to canned inputs
- pytest exit status flows back through the runner workflow

These tests deliberately hit only the fakes — no Jarvis services required —
so the runner workflow can prove the loop without standing up a stack.
"""

from __future__ import annotations

import os

import httpx
import pytest

FAKE_LLM_URL = os.environ.get("FAKE_LLM_URL", "http://127.0.0.1:7705")
FAKE_WHISPER_URL = os.environ.get("FAKE_WHISPER_URL", "http://127.0.0.1:7706")


@pytest.mark.qa_case("CASE-001")
def test_fake_llm_returns_canned_completion():
    response = httpx.post(
        f"{FAKE_LLM_URL}/v1/chat",
        json={
            "messages": [{"role": "user", "content": "What's 25 plus 37?"}],
            "model": "fake-llm",
        },
        timeout=5.0,
    )
    response.raise_for_status()
    body = response.json()
    assert body["message"]["content"] == "The result is 62."
    assert body["message"]["stop_reason"] == "complete"


@pytest.mark.qa_case("CASE-002")
def test_fake_llm_emits_tool_call_for_timer_prompt():
    response = httpx.post(
        f"{FAKE_LLM_URL}/v1/chat",
        json={
            "messages": [{"role": "user", "content": "Set a 5 minute timer"}],
            "model": "fake-llm",
        },
        timeout=5.0,
    )
    response.raise_for_status()
    body = response.json()
    assert body["message"]["stop_reason"] == "tool_calls"
    assert body["message"]["tool_calls"][0]["function"]["name"] == "set_timer"


@pytest.mark.qa_case("CASE-003")
def test_fake_whisper_returns_canned_transcript_for_known_filename():
    files = {"audio": ("timer_test.wav", b"\x00" * 16, "audio/wav")}
    response = httpx.post(
        f"{FAKE_WHISPER_URL}/transcribe",
        files=files,
        timeout=5.0,
    )
    response.raise_for_status()
    body = response.json()
    assert body["text"] == "Set a five minute timer"
    assert body["fake"] is True
