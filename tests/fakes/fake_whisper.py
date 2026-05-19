"""Fake Whisper backend for the integration-runner workflow.

FastAPI shim that mirrors the wire format of jarvis-whisper-api. Returns
canned transcripts based on the uploaded audio filename (regex match).
Unmatched filenames return a generic stub. No real audio decoding or GPU.

Start standalone (the CI workflow runs it this way):

    python -m tests.fakes.fake_whisper --port 7706 \\
        --responses tests/fakes/canned_responses.yaml

Override at runtime via env: FAKE_WHISPER_PORT, FAKE_WHISPER_RESPONSES.
"""

from __future__ import annotations

import argparse
import os
import re
from pathlib import Path

import uvicorn
import yaml
from fastapi import FastAPI, File, UploadFile

DEFAULT_PORT = int(os.environ.get("FAKE_WHISPER_PORT", "7706"))
DEFAULT_RESPONSES = Path(
    os.environ.get(
        "FAKE_WHISPER_RESPONSES",
        str(Path(__file__).parent / "canned_responses.yaml"),
    )
)

app = FastAPI()
_canned: list[dict] = []


def _load_transcripts(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text()) or {}
    return data.get("transcripts", []) or []


def _match(filename: str) -> str:
    for entry in _canned:
        pattern = entry.get("filename_regex")
        if pattern and re.search(pattern, filename, re.IGNORECASE):
            return entry["transcript"]
    return "fake transcript"


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)):
    # Field name MUST be `file` to match real jarvis-whisper-api
    # (app/main.py:127) and CC's WhisperClient.transcribe()
    # (jarvis-command-center/app/core/clients/whisper_client.py),
    # which posts files={"file": (filename, audio)}. Earlier revs
    # used `audio` here which silently worked only because the
    # CASE-003 smoke test also sent `audio` — the moment CC's media
    # proxy got plumbed in (CASE-208) the mismatch surfaced.
    transcript = _match(file.filename or "")
    return {
        "text": transcript,
        "speaker": {"user_id": None, "confidence": 0.0},
        "fake": True,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "fake": True, "transcript_count": len(_canned)}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--responses", type=Path, default=DEFAULT_RESPONSES)
    args = parser.parse_args()
    global _canned
    _canned = _load_transcripts(args.responses)
    # Bind to 0.0.0.0 so CI containers can reach us via host.docker.internal.
    # Loopback-only would only be reachable from the GHA runner host process,
    # not from inside the CC container.
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
