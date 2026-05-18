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
    for entry in _canned:
        pattern = entry.get("prompt_regex")
        if pattern and re.search(pattern, prompt, re.IGNORECASE):
            return entry["response"]
    return {
        "role": "assistant",
        "content": "OK",
        "stop_reason": "complete",
    }


@app.post("/v1/chat")
async def chat(req: ChatRequest):
    user_prompt = ""
    for msg in reversed(req.messages):
        if msg.get("role") == "user":
            user_prompt = msg.get("content", "") or ""
            break
    return {
        "id": "fake-llm-001",
        "model": req.model or "fake-llm",
        "message": _match(user_prompt),
    }


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
