"""Fake LLM backend for the integration-runner workflow.

FastAPI shim that mirrors the wire format of jarvis-llm-proxy-api so cross-
service integration tests can run on free Linux runners without GPU or real
model weights. Prompts are matched against `canned_responses.yaml` by regex
(first match wins); unmatched prompts fall back to a generic stub.

Start standalone (the CI workflow runs it this way):

    python -m tests.fakes.fake_llm_backend --port 7705 \\
        --responses tests/fakes/canned_responses.yaml

Override at runtime via env: FAKE_LLM_PORT, FAKE_LLM_RESPONSES.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI
from pydantic import BaseModel

DEFAULT_PORT = int(os.environ.get("FAKE_LLM_PORT", "7705"))
DEFAULT_RESPONSES = Path(
    os.environ.get(
        "FAKE_LLM_RESPONSES",
        str(Path(__file__).parent / "canned_responses.yaml"),
    )
)

app = FastAPI()
_canned: list[dict] = []


class ChatRequest(BaseModel):
    messages: list[dict]
    tools: list[dict] | None = None
    model: str | None = None


def _load_responses(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("responses", []) or []


def _match(prompt: str) -> dict:
    """Find canned response for a user prompt. Returns the simplified
    canned-yaml shape (role/content/stop_reason/tool_calls)."""
    for entry in _canned:
        pattern = entry.get("prompt_regex")
        if pattern and re.search(pattern, prompt, re.IGNORECASE):
            return entry["response"]
    return {
        "role": "assistant",
        "content": "OK",
        "stop_reason": "complete",
    }


def _to_openai(canned: dict, model: str) -> dict:
    """Translate the canned-yaml shape into OpenAI chat-completion shape,
    which is what the real jarvis-llm-proxy-api emits and what CC parses
    (CC reads `choices[0].message` + `choices[0].finish_reason`; see
    jarvis-command-center/app/core/tool_execution_engine.py:605-613).

    Canned `stop_reason` values map to OpenAI `finish_reason`:
      complete       → stop
      tool_calls     → tool_calls
      anything else  → passed through verbatim
    """
    stop_reason = canned.get("stop_reason", "complete")
    finish_reason = "stop" if stop_reason == "complete" else stop_reason

    message: dict = {
        "role": canned.get("role", "assistant"),
        "content": canned.get("content", "") or "",
    }
    tool_calls = canned.get("tool_calls")
    if tool_calls:
        # Real proxy emits each tool_call with a top-level `type: function`
        # field; canned entries don't carry that, so add it here for parity.
        message["tool_calls"] = [
            {
                "id": tc.get("id"),
                "type": tc.get("type", "function"),
                "function": tc.get("function", {}),
            }
            for tc in tool_calls
        ]

    return {
        "id": "fake-llm-001",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    """OpenAI-style endpoint matching what jarvis-llm-proxy-api exposes
    and what CC's LLMProxyClient targets via JARVIS_LLM_PROXY_API_URL."""
    user_prompt = ""
    for msg in reversed(req.messages):
        if msg.get("role") == "user":
            user_prompt = msg.get("content", "") or ""
            break
    canned = _match(user_prompt)
    return _to_openai(canned, req.model or "fake-llm")


@app.get("/health")
async def health():
    return {"status": "ok", "fake": True, "canned_count": len(_canned)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--responses", type=Path, default=DEFAULT_RESPONSES)
    args = parser.parse_args()
    global _canned
    _canned = _load_responses(args.responses)
    # Bind to 0.0.0.0 so CI containers can reach us via host.docker.internal.
    # Loopback-only would only be reachable from the GHA runner host process,
    # not from inside the CC container.
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
