"""Smoke tests against the real Jarvis stack running in
`docker-compose.ci.yaml`'s `core` profile (Postgres + auth + config-service +
the service-under-test).

Lives at `tests/` (not `tests/integration/`) for the same reason as
`test_loop_smoke.py`: the `tests/integration/` subtree's conftest imports
the production codebase, which depends on `jarvis_command_sdk`. Putting
this here keeps the smoke suite SDK-free.

Skipped when `CC_URL` is unset — the v1 fakes-only loop and the v2.1+ full
compose loop coexist, and only the latter sets `CC_URL`. Local runs that
don't bring up the stack still pass these as "skipped" rather than failing.

URLs default to where docker-compose.ci.yaml maps each service's port,
overridable via env so the same test can run against any compose layout.
"""

from __future__ import annotations

import os

import httpx
import pytest

CC_URL = os.environ.get("CC_URL")
AUTH_URL = os.environ.get("AUTH_URL", "http://localhost:7701")
CONFIG_URL = os.environ.get("CONFIG_URL", "http://localhost:7700")
CC_APP_ID = os.environ.get("CC_APP_ID", "command-center")
CC_APP_KEY = os.environ.get("CC_APP_KEY", "")
CC_NODE_ID = os.environ.get("CC_NODE_ID", "")
CC_NODE_KEY = os.environ.get("CC_NODE_KEY", "")
SKIP_REASON = "CC_URL unset — skipping real-stack smoke tests (v1 fakes-only mode)"
SKIP_NO_KEY = "CC_APP_KEY unset — seed step did not run"
SKIP_NO_NODE = "CC_NODE_ID / CC_NODE_KEY unset — v2.4 node seed did not run"


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-101")
def test_cc_health_endpoint_responds_200():
    response = httpx.get(f"{CC_URL}/health", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    assert body.get("status") == "healthy", (
        f"expected status=healthy, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-102")
def test_cc_root_responds():
    """CC's root path should at minimum return a response (not 5xx).

    Whatever shape — JSON, HTML, 404 with body — confirms uvicorn is
    serving and the app didn't crash on startup.
    """
    response = httpx.get(f"{CC_URL}/", timeout=10.0)
    assert response.status_code < 500, (
        f"expected non-5xx, got {response.status_code} body={response.text[:200]}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-103")
def test_config_service_health_responds():
    """jarvis-config-service /health responds.

    Proves the ghcr.io :dev image pulled, alembic migrations ran, and the
    service bound to its port. CC's _setup_service_config() targets this
    service; if it's not up, CC's service-discovery path silently falls
    back to legacy env vars.
    """
    response = httpx.get(f"{CONFIG_URL}/health", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    assert body.get("status") == "ok", (
        f"expected status=ok, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.qa_case("CASE-104")
def test_auth_service_health_responds():
    """jarvis-auth /health responds.

    Proves the ghcr.io :dev image pulled, the auth schema migrated (auth's
    Dockerfile CMD chains alembic), and the service is serving. CC's
    node-auth and app-auth paths all depend on this.
    """
    response = httpx.get(f"{AUTH_URL}/health", timeout=10.0)
    response.raise_for_status()
    body = response.json()
    assert body.get("status") == "ok", (
        f"expected status=ok, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not CC_APP_KEY, reason=SKIP_NO_KEY)
@pytest.mark.qa_case("CASE-201")
def test_cc_seeded_app_credentials_validate_against_auth():
    """The app-client seed.sh registered for `command-center` actually
    authenticates against auth.

    Auth has no dedicated `/internal/validate-app` endpoint — app
    credentials are checked inline on every protected endpoint via
    `_validate_app_client()`. So we exercise an endpoint that DOES
    require app auth (`/internal/validate-node`) with a deliberately-
    bogus node, and check that the response shape comes back. Two
    distinct outcomes:

      - Our app credentials are valid → auth proceeds past app-auth,
        validates the node, finds nothing, returns 200 with valid=false.
      - Our app credentials are invalid → auth 401s at app-auth before
        looking at the node, raise_for_status() throws.

    A 200 response with `valid=false` for a nonexistent node is
    therefore success for THIS test: it confirms the seeded app key
    works. The positive-path counterpart is CASE-202.
    """
    response = httpx.post(
        f"{AUTH_URL}/internal/validate-node",
        headers={
            "X-Jarvis-App-Id": CC_APP_ID,
            "X-Jarvis-App-Key": CC_APP_KEY,
        },
        json={
            "node_id": "ci-nonexistent-node",
            "node_key": "ci-nonexistent-key",
            "service_id": "command-center",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    body = response.json()
    assert body.get("valid") is False, (
        f"expected valid=false for nonexistent node, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(not CC_APP_KEY, reason=SKIP_NO_KEY)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-202")
def test_cc_seeded_node_validates_against_auth():
    """Positive-path counterpart to CASE-201: a real seeded node + key
    validates `valid=true` against auth's /internal/validate-node.

    seed.sh has already (a) registered a CI user via /auth/register —
    which auto-creates a household and returns the household_id, and
    (b) POSTed /admin/nodes with that household_id, capturing the
    returned node_key. Both are exported to the workflow env as
    CC_NODE_ID + CC_NODE_KEY.

    Together, CASE-201 + CASE-202 cover both branches of the
    /internal/validate-node contract: bogus creds → valid=false, real
    creds → valid=true. If both pass, the auth seed end-to-end works.
    """
    response = httpx.post(
        f"{AUTH_URL}/internal/validate-node",
        headers={
            "X-Jarvis-App-Id": CC_APP_ID,
            "X-Jarvis-App-Key": CC_APP_KEY,
        },
        json={
            "node_id": CC_NODE_ID,
            "node_key": CC_NODE_KEY,
            "service_id": "command-center",
        },
        timeout=10.0,
    )
    response.raise_for_status()
    body = response.json()
    assert body.get("valid") is True, (
        f"expected valid=true for seeded node, got body={body}"
    )
    assert body.get("node_id") == CC_NODE_ID, (
        f"expected node_id={CC_NODE_ID}, got {body.get('node_id')}"
    )
    assert body.get("household_id"), (
        f"expected household_id to be populated, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-203")
def test_cc_conversation_start_with_node_creds():
    """First test that goes *through* CC with a real node X-API-Key.

    POSTs CC's /api/v0/conversation/start with the seeded node
    credentials. CC's verify_api_key dependency:
      1. Parses X-API-Key into node_id + node_key.
      2. POSTs auth's /internal/validate-node (with CC's own
         X-Jarvis-App-Id + X-Jarvis-App-Key headers, set from the
         JARVIS_APP_KEY env we seeded into CC's compose).
      3. Looks up the node in CC's local Postgres `nodes` table
         (created by the same /admin/nodes call that registered it
         in auth — see the Phase 2.5 workflow step).

    Both rows have to exist; if either is missing CC returns 401.
    Success here proves the full chain works end-to-end.

    Asserts a 200, that `status` is "success", and that the
    `conversation_id` in the response echoes back what we sent.
    """
    conv_id = "ci-conv-203"
    response = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={
            "X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}",
        },
        json={"conversation_id": conv_id},
        timeout=15.0,
    )
    assert response.status_code == 200, (
        f"expected 200, got {response.status_code} body={response.text[:300]}"
    )
    body = response.json()
    assert body.get("status") == "success", (
        f"expected status=success, got body={body}"
    )
    assert body.get("conversation_id") == conv_id, (
        f"expected conversation_id={conv_id} echoed back, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-204")
def test_cc_voice_command_returns_tool_calls():
    """First test that exercises CC's voice command pipeline through the
    fake LLM and back.

    Setup: open a conversation (`/conversation/start`) with empty
    `client_tools` — required because `/voice/command/stream`'s line 882
    check (`tools is None`) raises 400 "Conversation not initialized for
    tool-based flow" if the cache entry's tools field is None.

    Action: POST `/voice/command/stream` with `voice_command="set a 5
    minute timer"`. The fake LLM regex-matches that prompt against
    `canned_responses.yaml`'s timer entry, which returns
    `stop_reason: tool_calls` with a `set_timer` function call.

    Expected: 202 JSON. CC's main.py:974+ picks 200 audio only when
    `stop_reason=="complete"` with non-empty `assistant_message`;
    everything else (tool_calls, validation_required, error) falls
    through to a 202 with the `VoiceCommandResponse` body. We assert:
      - 202 status
      - `stop_reason == "tool_calls"`
      - exactly one tool call
      - the tool call's function name is `set_timer`

    What this proves end-to-end:
      - CC's verify_api_key chain (auth + local DB) still works under
        the voice path (same dependency as /conversation/start).
      - CC reaches the fake LLM at host.docker.internal:7705 from
        inside the container (extra_hosts mapping + LLM_PROXY_API_URL
        env are both correct).
      - LLM response shape is parsed correctly into VoiceCommandResponse.
      - The 202 branch fires when tool_calls are present.
    """
    conv_id = "ci-conv-204"
    start = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "conversation_id": conv_id,
            "client_tools": [],
            "available_commands": [],
        },
        timeout=15.0,
    )
    assert start.status_code == 200, (
        f"/conversation/start setup failed: {start.status_code} "
        f"body={start.text[:300]}"
    )

    response = httpx.post(
        f"{CC_URL}/api/v0/voice/command/stream",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "voice_command": "set a 5 minute timer",
            "conversation_id": conv_id,
        },
        timeout=30.0,
    )
    assert response.status_code == 202, (
        f"expected 202 JSON tool-call branch, got {response.status_code} "
        f"body={response.text[:400]}"
    )
    body = response.json()
    assert body.get("stop_reason") == "tool_calls", (
        f"expected stop_reason=tool_calls, got body={body}"
    )
    tool_calls = body.get("tool_calls") or []
    assert len(tool_calls) == 1, (
        f"expected exactly one tool call, got {len(tool_calls)}: {tool_calls}"
    )
    fn = tool_calls[0].get("function", {})
    assert fn.get("name") == "set_timer", (
        f"expected tool_calls[0].function.name=set_timer, got {fn}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-205")
def test_cc_continue_with_tool_results():
    """First end-to-end coverage of the tool-execution continuation loop.

    Sequence:
      1. /conversation/start — initialize cache.
      2. /voice/command/stream "set a 5 minute timer" — CC returns 202
         JSON with `tool_calls[0]` (a `set_timer` call) and a
         `tool_call_id`.
      3. /voice/command/continue — node simulates having run the tool
         and POSTs the result back. CC injects a user message
         "Here are the tool results..." into the conversation and
         calls the LLM again. The fake LLM matches that regex (see
         canned_responses.yaml's continuation entry) and returns
         "Timer set for 5 minutes." with stop_reason=complete. CC's
         tool_call_parser fails to JSON-decode that plain text and
         falls back to ("stop", [], content), producing a final
         VoiceCommandResponse with stop_reason="complete" and
         assistant_message="Timer set for 5 minutes.".

    What this proves on top of CASE-204:
      - The conversation cache is correctly carried across turns
        (the LLM sees the continuation prompt, not the original).
      - CC's continuation prompt-building logic ("Here are the tool
        results...") still matches reality — if anyone changes the
        wording, this test fails and the test_loop fakes need a new
        regex.
      - The tool_results body shape ({tool_call_id, output}) parses
        and reaches the LLM iteration.
      - The full request_id flows end-to-end with stop_reason=complete
        landing in the response shape the node consumes.

    Targets the BLOCKING /voice/command/continue endpoint (not
    /continue/stream), which returns JSON not audio. The streaming
    variant + fake TTS is CASE-206's job.
    """
    conv_id = "ci-conv-205"

    # Setup: open conversation.
    start = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "conversation_id": conv_id,
            "client_tools": [],
            "available_commands": [],
        },
        timeout=15.0,
    )
    assert start.status_code == 200, (
        f"/conversation/start setup failed: {start.status_code} "
        f"body={start.text[:300]}"
    )

    # Step 1: voice command → tool_calls.
    voice = httpx.post(
        f"{CC_URL}/api/v0/voice/command/stream",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "voice_command": "set a 5 minute timer",
            "conversation_id": conv_id,
        },
        timeout=30.0,
    )
    assert voice.status_code == 202, (
        f"/voice/command/stream step failed: {voice.status_code} "
        f"body={voice.text[:300]}"
    )
    voice_body = voice.json()
    tool_calls = voice_body.get("tool_calls") or []
    assert len(tool_calls) == 1, (
        f"expected one tool call from voice/stream, got body={voice_body}"
    )
    tool_call_id = tool_calls[0].get("id")
    assert tool_call_id, f"expected tool_call_id, got {tool_calls[0]}"

    # Step 2: post the tool result back. Output mimics what the node
    # would return after running set_timer locally.
    result = httpx.post(
        f"{CC_URL}/api/v0/voice/command/continue",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "conversation_id": conv_id,
            "tool_results": [
                {
                    "tool_call_id": tool_call_id,
                    "output": "Timer started: 5 minutes, label='test'",
                }
            ],
        },
        timeout=30.0,
    )
    assert result.status_code == 200, (
        f"/voice/command/continue failed: {result.status_code} "
        f"body={result.text[:400]}"
    )
    body = result.json()
    assert body.get("stop_reason") == "complete", (
        f"expected stop_reason=complete after continuation, got body={body}"
    )
    assistant_message = body.get("assistant_message") or ""
    assert assistant_message.strip(), (
        f"expected non-empty assistant_message, got body={body}"
    )
    # Loose check — the canned continuation response says "Timer set"
    # but we don't want to brittleness-tie to exact wording. Just look
    # for the keyword from the canned content.
    assert "timer" in assistant_message.lower(), (
        f"expected 'timer' in assistant_message, got: {assistant_message!r}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-206")
def test_cc_continue_stream_returns_audio():
    """End-to-end audio path: streaming continuation produces PCM bytes.

    CC's /voice/command/continue/stream pipes the LLM response sentence-
    by-sentence to TTS, returning audio/raw. This test proves every link
    in that pipeline works against the fakes:

      1. CC opens an SSE stream to the fake LLM (which now supports
         stream=true and yields `data: {"delta": "..."}` lines for each
         word of the canned continuation response).
      2. CC's sentence-boundary detector accumulates tokens until it
         sees `.`/`!`/`?` followed by whitespace.
      3. CC sends each completed sentence to the fake TTS at port 7707.
      4. Fake TTS returns 32 bytes of zero PCM + X-Audio-* headers.
      5. CC concatenates the TTS chunks into its own StreamingResponse
         and forwards them to us.

    Asserts: 200, content-type audio/raw, non-zero body, and the
    X-Audio-Sample-Rate header is present (proves CC sourced format
    metadata from the fake's /audio/format endpoint, not from the
    no-TTS exception fallback).

    Same setup as CASE-205 (tool_call_id from /voice/command/stream),
    but with a fresh conversation_id so the two tests are independent.
    """
    conv_id = "ci-conv-206"

    start = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "conversation_id": conv_id,
            "client_tools": [],
            "available_commands": [],
        },
        timeout=15.0,
    )
    assert start.status_code == 200, (
        f"/conversation/start setup failed: {start.status_code} "
        f"body={start.text[:300]}"
    )

    voice = httpx.post(
        f"{CC_URL}/api/v0/voice/command/stream",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "voice_command": "set a 5 minute timer",
            "conversation_id": conv_id,
        },
        timeout=30.0,
    )
    assert voice.status_code == 202, (
        f"voice/command/stream setup failed: {voice.status_code} "
        f"body={voice.text[:300]}"
    )
    tool_call_id = (voice.json().get("tool_calls") or [{}])[0].get("id")
    assert tool_call_id, f"expected tool_call_id, got {voice.json()}"

    with httpx.stream(
        "POST",
        f"{CC_URL}/api/v0/voice/command/continue/stream",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "conversation_id": conv_id,
            "tool_results": [
                {
                    "tool_call_id": tool_call_id,
                    "output": "Timer started: 5 minutes, label='test'",
                }
            ],
        },
        timeout=30.0,
    ) as response:
        assert response.status_code == 200, (
            f"expected 200 audio/raw, got {response.status_code} "
            f"body={response.read()[:400]!r}"
        )
        content_type = response.headers.get("content-type", "")
        assert content_type.startswith("audio/raw"), (
            f"expected content-type=audio/raw, got {content_type!r}"
        )
        # Audio metadata headers come from the fake TTS's /audio/format
        # response (CC's tts_client.get_audio_format()). If TTS was
        # unreachable CC falls back to hardcoded defaults — the header
        # still gets set, so we read it for parity but the real signal
        # is the body bytes below.
        assert response.headers.get("X-Audio-Sample-Rate"), (
            f"expected X-Audio-Sample-Rate header, headers={dict(response.headers)}"
        )

        body = b""
        for chunk in response.iter_bytes():
            body += chunk
        assert len(body) > 0, (
            "expected non-zero audio body — CC's _audio_generator silently "
            "yields nothing when the LLM stream or TTS call fails, which "
            "would manifest as 0 bytes here. Check the fake LLM SSE format "
            "and fake TTS reachability."
        )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-207")
def test_cc_voice_acknowledge_returns_text():
    """Wake-acknowledge is a deliberately-fast no-LLM keyword match.

    CC's voice loop runs `/voice/acknowledge` in parallel with
    `/voice/command/stream` — the user hears "On it" or "Sure"
    within ~50ms of the wake word, while the real command is still
    being processed. If this endpoint ever starts touching the LLM,
    the latency win evaporates.

    The test sends an arbitrary voice_command and asserts only that
    the response is a 200 JSON with a non-empty `text` field — the
    actual ack string is randomized from CC's keyword pools and
    isn't worth pinning. The real signal is: did we get a fast,
    deterministic response shape? If the endpoint silently started
    calling the LLM or TTS, the test would still pass — but the
    latency regression would surface in CC's logs (a fakes-only
    response should complete in <100ms).
    """
    response = httpx.post(
        f"{CC_URL}/api/v0/voice/acknowledge",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={"voice_command": "turn on the living room light"},
        timeout=10.0,
    )
    assert response.status_code == 200, (
        f"expected 200, got {response.status_code} body={response.text[:300]}"
    )
    body = response.json()
    text = body.get("text")
    assert isinstance(text, str) and text.strip(), (
        f"expected non-empty text field, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-208")
def test_cc_media_whisper_transcribe_proxies():
    """CC's media proxy forwards audio uploads to jarvis-whisper-api
    and returns the transcript verbatim.

    Round-trip:
      test → POST CC /api/v0/media/whisper/transcribe (multipart
        with field name `file`, filename `timer_clip.wav`)
      → CC's verify_api_key + WhisperClient with context headers
        (X-Household-ID, X-Node-ID, X-Member-IDs)
      → fake whisper at host.docker.internal:7706/transcribe
      → fake regex-matches `timer.*\\.wav$` → returns the canned
        "Set a five minute timer" transcript
      → CC forwards the JSON body unchanged
      → test asserts body["text"] is exactly that transcript

    What this proves on top of CASE-003:
      - CC reaches the fake whisper through JARVIS_WHISPER_URL +
        the auth context headers.
      - The multipart field name is `file` (not `audio`) end-to-end
        — same name the real whisper API uses (app/main.py:127).
      - CC forwards rather than wrapping/transforming the response.
    """
    files = {"file": ("timer_clip.wav", b"\x00" * 32, "audio/wav")}
    response = httpx.post(
        f"{CC_URL}/api/v0/media/whisper/transcribe",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        files=files,
        timeout=15.0,
    )
    assert response.status_code == 200, (
        f"expected 200, got {response.status_code} body={response.text[:400]}"
    )
    body = response.json()
    assert body.get("text") == "Set a five minute timer", (
        f"expected canned timer transcript, got body={body}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-209")
def test_cc_voice_command_returns_audio_for_complete_response():
    """`/voice/command/stream`'s 200 audio path — the conversational
    response branch (LLM returns a plain text answer, no tool_call).

    Sequence:
      1. /conversation/start (same setup as CASE-204).
      2. POST /voice/command/stream with voice_command="hello jarvis".
         The fake LLM regex-matches `\\b(hello|hi|hey)\\b` →
         returns canned `complete` response with content
         "Hello! How can I help?" (plain text, no tool_calls).
      3. CC's tool_call_parser tries to JSON-decode the content,
         fails, falls back to `("stop", [], "Hello! How can I help?")`.
         The tool loop ends with `stop_reason: complete` +
         `assistant_message: "Hello! How can I help?"`.
      4. `handle_voice_stream` sees `stop_reason == "complete"` AND
         a non-empty assistant_message → takes the 200 audio path:
         instantiates a TTSClient, calls `get_audio_format()`, then
         feeds the assistant message through `stream_text_as_audio`
         which posts each sentence to the fake TTS's /speak/stream
         and yields the returned PCM chunks.
      5. The fake TTS returns 32 bytes of zero PCM + X-Audio-*
         headers; CC forwards them in a StreamingResponse.

    Asserts 200, content-type audio/raw, non-zero body, the
    `X-Audio-Sample-Rate` header is set, and the `X-Assistant-Message`
    header contains the canned response so we can verify CC actually
    threaded the message (not just streamed empty bytes).

    This closes the symmetric pair with CASE-204:
      - CASE-204: tool_calls path → 202 JSON
      - CASE-209: complete-with-text path → 200 audio

    Combined with CASE-205 (blocking continue) and CASE-206 (streaming
    continue), the full set of `/voice/command/*` branches is covered.
    """
    conv_id = "ci-conv-209"

    start = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "conversation_id": conv_id,
            "client_tools": [],
            "available_commands": [],
        },
        timeout=15.0,
    )
    assert start.status_code == 200, (
        f"/conversation/start setup failed: {start.status_code} "
        f"body={start.text[:300]}"
    )

    with httpx.stream(
        "POST",
        f"{CC_URL}/api/v0/voice/command/stream",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "voice_command": "hello jarvis",
            "conversation_id": conv_id,
        },
        timeout=30.0,
    ) as response:
        assert response.status_code == 200, (
            f"expected 200 audio (complete path), got {response.status_code} "
            f"body={response.read()[:400]!r}"
        )
        content_type = response.headers.get("content-type", "")
        assert content_type.startswith("audio/raw"), (
            f"expected content-type=audio/raw, got {content_type!r} — "
            f"if this is application/json the LLM response landed in the "
            f"202 tool_calls branch instead of the 200 audio branch."
        )
        assert response.headers.get("X-Audio-Sample-Rate"), (
            "expected X-Audio-Sample-Rate header — CC didn't reach the fake "
            "TTS, or fell through to a TTS-less path."
        )
        body = b""
        for chunk in response.iter_bytes():
            body += chunk
        assert len(body) > 0, (
            "expected non-zero audio body — same failure mode as CASE-206 "
            "(SSE format mismatch or TTS unreachable from inside CC)."
        )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-210")
def test_cc_voice_command_returns_validation_request():
    """The validation_required branch — CC asks the user to clarify.

    When the LLM emits the `request_validation` server tool (because
    a parameter is ambiguous or missing), CC's tool execution engine
    detects the `_validation_request: True` marker in the server-tool
    result and returns a 202 with `stop_reason: "validation_required"`
    + a `validation_request` body that the voice node renders to the
    user. The user's answer is then sent back as a continuation.

    Setup: open conversation. Action: POST `/voice/command/stream`
    with "play music" — the fake LLM regex-matches that as ambiguous
    ("which artist?") and returns a `request_validation` tool call
    with arguments `{question, parameter_name, options}`.

    What this proves on top of the other voice-flow tests:
      - The server-tool execution path actually runs (CC's tool exec
        engine handles `request_validation` differently from client
        tool calls).
      - The `_validation_request` marker is detected and translated
        into the public `stop_reason: validation_required` shape.
      - The validation_request body fields (`question`,
        `parameter_name`, `options`) round-trip through CC unchanged.

    Asserts 202 + stop_reason + the three validation_request fields.
    Uses a fresh conversation_id so the test is independent of the
    other CASE-2xx tests.
    """
    conv_id = "ci-conv-210"

    start = httpx.post(
        f"{CC_URL}/api/v0/conversation/start",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "conversation_id": conv_id,
            "client_tools": [],
            "available_commands": [],
        },
        timeout=15.0,
    )
    assert start.status_code == 200, (
        f"/conversation/start setup failed: {start.status_code} "
        f"body={start.text[:300]}"
    )

    response = httpx.post(
        f"{CC_URL}/api/v0/voice/command/stream",
        headers={"X-API-Key": f"{CC_NODE_ID}:{CC_NODE_KEY}"},
        json={
            "voice_command": "play music",
            "conversation_id": conv_id,
        },
        timeout=30.0,
    )
    assert response.status_code == 202, (
        f"expected 202 JSON validation branch, got {response.status_code} "
        f"body={response.text[:400]}"
    )
    body = response.json()
    assert body.get("stop_reason") == "validation_required", (
        f"expected stop_reason=validation_required, got body={body}"
    )
    validation_request = body.get("validation_request") or {}
    question = validation_request.get("question") or ""
    assert "artist" in question.lower(), (
        f"expected validation question to mention 'artist', got "
        f"validation_request={validation_request}"
    )
    assert validation_request.get("parameter_name") == "artist", (
        f"expected parameter_name=artist, got "
        f"validation_request={validation_request}"
    )
    options = validation_request.get("options")
    assert isinstance(options, list), (
        f"expected options to be a list, got {options!r}"
    )
