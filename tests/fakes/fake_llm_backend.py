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
import json
import os
import re
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
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
    stream: bool | None = False


def _load_responses(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("responses", []) or []


def _match(prompt: str, messages: list[dict]) -> dict:
    """Find canned response for a user prompt. Returns the simplified
    canned-yaml shape (role/content/stop_reason/tool_calls).

    Honors optional `requires_tool_message` flag on entries: when True,
    the entry only matches if `messages` already contains a tool-role
    message. This lets a single test exercise multi-iteration tool
    loops where CC re-calls the LLM after running a server tool —
    the second-iteration response uses `requires_tool_message: True`
    to differentiate from the first.
    """
    has_tool_message = any(m.get("role") == "tool" for m in messages)
    for entry in _canned:
        pattern = entry.get("prompt_regex")
        if not pattern or not re.search(pattern, prompt, re.IGNORECASE):
            continue
        if entry.get("requires_tool_message") and not has_tool_message:
            continue
        return entry["response"]
    return {
        "role": "assistant",
        "content": "OK",
        "stop_reason": "complete",
    }


def _to_openai(canned: dict, model: str) -> dict:
    """Translate the canned-yaml shape into OpenAI chat-completion shape,
    which is what the real jarvis-llm-proxy-api emits.

    Two paths, both real:

    1. **Plain-text response.** Canned `content` is plain prose with
       `stop_reason: complete` → emit as `choices[0].message.content`
       with `finish_reason: "stop"`. CC's text-based parser
       (tool_call_parser.parse_response) fails to JSON-decode it and
       falls back to "stop" + the raw content as assistant_message.

    2. **Tool-call response.** Canned `tool_calls` is present → emit
       the tool calls as a JSON string in `message.content`:

           {"message": "", "tool_calls": [{"name": ..., "arguments": {...}}]}

       Plus `finish_reason: "stop"`. CC's parser JSON-decodes the
       content, finds `tool_calls`, and returns
       `("tool_calls", [...], message)`. This is the path the real
       adapter-trained models use too — they emit JSON in content
       (LoRA-trained on that exact shape) and the proxy returns it
       verbatim. We're not using native OpenAI `message.tool_calls`
       because CC's `use_native_tools` is False without a
       JarvisAdapterModel prompt provider registered, which makes
       CC ignore native tool_calls and only parse content as JSON.
    """
    role = canned.get("role", "assistant")
    tool_calls = canned.get("tool_calls")

    if tool_calls:
        content = json.dumps({
            "message": canned.get("content", "") or "",
            "tool_calls": [
                {
                    "name": tc.get("function", {}).get("name"),
                    "arguments": _coerce_arguments(
                        tc.get("function", {}).get("arguments", {})
                    ),
                }
                for tc in tool_calls
            ],
        })
        finish_reason = "stop"
    else:
        content = canned.get("content", "") or ""
        stop_reason = canned.get("stop_reason", "complete")
        finish_reason = "stop" if stop_reason == "complete" else stop_reason

    return {
        "id": "fake-llm-001",
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": role, "content": content},
                "finish_reason": finish_reason,
            }
        ],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _coerce_arguments(raw: object) -> dict:
    """Canned `arguments` may be a JSON string (as written in YAML for
    readability) or already a dict. CC's parser expects a dict."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


def _sse_stream_for(canned: dict):
    """SSE generator emitting `data: {"delta": "<chunk>"}\\n\\n` events
    followed by `data: {"done": true, ...}\\n\\n`. Matches what
    jarvis-command-center's `chat_completion_stream` parses (see
    jarvis-command-center/app/core/llm_proxy_client.py:157-169).

    We emit one delta per word so CC's sentence-boundary detector
    (re-split on `[.!?]\\s+`) actually triggers — a single mega-delta
    would never hit the boundary and TTS would never be called.

    Only used for the plain-text path; tool-call streaming has its own
    chunked format and we don't have a test covering it yet.
    """
    text = canned.get("content", "") or ""
    if not text:
        # No content to stream — just emit a done event so the consumer
        # sees a clean termination.
        yield b'data: {"done": true}\n\n'
        return
    for word in text.split(" "):
        # The space after each word is what triggers the sentence
        # boundary regex when the word ends with . / ! / ?.
        chunk = word + " "
        payload = json.dumps({"delta": chunk})
        yield f"data: {payload}\n\n".encode("utf-8")
    final = json.dumps({"done": True, "content": text})
    yield f"data: {final}\n\n".encode("utf-8")


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest):
    """OpenAI-style endpoint matching what jarvis-llm-proxy-api exposes
    and what CC's LLMProxyClient targets via JARVIS_LLM_PROXY_API_URL.

    Branches on `stream` field in the request body:
      - false/missing → returns the standard JSON shape (used by CC's
        non-streaming `chat_completion` call — CASE-001/002/204/205).
      - true          → returns SSE-formatted events (used by CC's
        `chat_completion_stream` for the streaming voice paths —
        CASE-206 and the future /voice/command/stream 200-audio branch).
    """
    user_prompt = ""
    for msg in reversed(req.messages):
        if msg.get("role") == "user":
            user_prompt = msg.get("content", "") or ""
            break
    canned = _match(user_prompt, req.messages)

    if req.stream:
        return StreamingResponse(
            _sse_stream_for(canned),
            media_type="text/event-stream",
        )
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
