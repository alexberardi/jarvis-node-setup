"""Fake TTS backend for the integration-runner workflow.

FastAPI shim that mirrors the wire format of jarvis-tts so cross-service
integration tests can exercise CC's audio path without a real Piper voice
or GPU. Returns a small, deterministic chunk of zero-PCM for any text —
the test just needs to see *some* audio bytes plus the correct format
headers, not real synthesized speech.

Start standalone (the CI workflow runs it this way):

    python -m tests.fakes.fake_tts --port 7707

Endpoints (subset of the real jarvis-tts):
    POST /speak/stream     → audio/raw, X-Audio-* headers
    GET  /audio/format     → {sample_rate, channels, sample_width}
    GET  /health           → {status: ok, fake: true}

The real jarvis-tts requires app-to-app auth (X-Jarvis-App-Id +
X-Jarvis-App-Key). We don't validate those headers — the test loop's
goal is to prove the wire shape, not to re-test auth (that's CASE-201/
202's job).
"""

from __future__ import annotations

import argparse
import os

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

DEFAULT_PORT = int(os.environ.get("FAKE_TTS_PORT", "7707"))

# Deterministic 22.05kHz / mono / 16-bit PCM — matches the real jarvis-tts
# default Piper voice. CC reads these headers via tts_client.get_audio_format()
# (clients/tts_client.py:152) and via the response of /speak/stream
# (clients/tts_client.py:130-134).
_SAMPLE_RATE = 22050
_CHANNELS = 1
_SAMPLE_WIDTH = 2  # bytes per sample

# 32 bytes of zero PCM is enough to prove "audio flowed end-to-end"
# without bloating the response. CC's StreamingResponse will forward
# whatever chunks the fake yields.
_AUDIO_CHUNK = b"\x00" * 32

app = FastAPI()


@app.get("/health")
async def health():
    return {"status": "ok", "fake": True}


@app.get("/audio/format")
async def audio_format():
    """Return the format metadata CC's tts_client polls before streaming.
    Real jarvis-tts builds this from the active provider; we just hard-
    code the Piper defaults the real service usually surfaces."""
    return {
        "sample_rate": _SAMPLE_RATE,
        "channels": _CHANNELS,
        "sample_width": _SAMPLE_WIDTH,
    }


@app.post("/speak/stream")
async def speak_stream(request: Request):
    """Stream raw PCM audio chunks for low-latency playback.

    Real implementation iterates `provider.synthesize(text)` chunk-by-
    chunk. Our fake just yields one fixed chunk of zero-PCM so the test
    sees a non-empty body plus the correct headers. CC's
    speak_stream client (clients/tts_client.py:121-144) reads the
    chunks via aiter_bytes — single-yield works fine.
    """
    # Drain the request body so CC's writer side completes. We don't
    # actually look at the text content.
    try:
        await request.json()
    except Exception:
        pass

    def pcm_generator():
        yield _AUDIO_CHUNK

    return StreamingResponse(
        pcm_generator(),
        media_type="audio/raw",
        headers={
            "X-Audio-Sample-Rate": str(_SAMPLE_RATE),
            "X-Audio-Channels": str(_CHANNELS),
            "X-Audio-Sample-Width": str(_SAMPLE_WIDTH),
            "X-Audio-Provider": "fake",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()
    # Bind to 0.0.0.0 so CC containers can reach us via host.docker.internal.
    uvicorn.run(app, host="0.0.0.0", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
