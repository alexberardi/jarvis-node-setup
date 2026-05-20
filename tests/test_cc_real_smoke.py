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
CC_USER_JWT = os.environ.get("CC_USER_JWT", "")
MQTT_BROKER_URL = os.environ.get("MQTT_BROKER_URL", "tcp://127.0.0.1:1883")
SKIP_REASON = "CC_URL unset — skipping real-stack smoke tests (v1 fakes-only mode)"
SKIP_NO_KEY = "CC_APP_KEY unset — seed step did not run"
SKIP_NO_NODE = "CC_NODE_ID / CC_NODE_KEY unset — v2.4 node seed did not run"
SKIP_NO_JWT = "CC_USER_JWT unset — v2.13 user-JWT seed did not run"


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


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-211")
def test_cc_voice_command_returns_multi_tool_calls():
    """Multi-tool flow — when the LLM emits 2+ tool_calls in one
    response, CC returns them all in a single 202.

    Why this matters: CC's tool execution engine
    (tool_execution_engine.py:827+) splits tool_calls into
    `server_results` (executed locally) + `client_calls` (returned
    to the node). When all calls are client tools, CC returns 202
    with the full client_calls list intact. When server + client
    mix, CC continues the loop. CASE-211 exercises the all-client
    branch; the mixed branch (server tool runs, then loop continues)
    is a future case.

    Setup: open conversation. Action: POST `/voice/command/stream`
    with "test multi-tool flow" — the fake LLM emits two tool_calls
    with names `client_tool_one` and `client_tool_two` (generic
    names that don't collide with any registered server tool, so
    both fall through to client_calls).

    What this proves on top of CASE-204:
      - CC's tool exec engine handles >1 tool_call per response
        without losing or reordering them.
      - The 202 response body's `tool_calls` field is a list, not
        a single object, and consumers can iterate it.
      - tool_call IDs are preserved so the node knows which result
        to send back for which call.

    Asserts 202, stop_reason==tool_calls, exactly two tool_calls,
    names in order (client_tool_one then client_tool_two), and
    both IDs come back intact.
    """
    conv_id = "ci-conv-211"

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
            "voice_command": "test multi-tool flow",
            "conversation_id": conv_id,
        },
        timeout=30.0,
    )
    assert response.status_code == 202, (
        f"expected 202 multi-tool branch, got {response.status_code} "
        f"body={response.text[:400]}"
    )
    body = response.json()
    assert body.get("stop_reason") == "tool_calls", (
        f"expected stop_reason=tool_calls, got body={body}"
    )
    tool_calls = body.get("tool_calls") or []
    assert len(tool_calls) == 2, (
        f"expected exactly two tool_calls, got {len(tool_calls)}: "
        f"{tool_calls}"
    )
    names = [tc.get("function", {}).get("name") for tc in tool_calls]
    assert names == ["client_tool_one", "client_tool_two"], (
        f"expected names in order [client_tool_one, client_tool_two], "
        f"got {names}"
    )
    ids = [tc.get("id") for tc in tool_calls]
    assert all(ids), f"expected all tool_calls to have ids, got {ids}"
    assert len(set(ids)) == 2, (
        f"expected distinct ids, got {ids}"
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.skipif(not CC_USER_JWT, reason=SKIP_NO_JWT)
@pytest.mark.qa_case("CASE-212")
def test_cc_publishes_to_mqtt_on_settings_request():
    """First test that exercises the server→node async channel (MQTT).

    Background: jarvis-node-setup's CLAUDE.md is explicit — "MQTT is
    the only server→node async channel". CC publishes to per-node
    topics for settings updates, TTS-by-text, package installs, etc.;
    the node's MQTT background thread subscribes and reacts.

    Sequence:
      1. Test subscribes to `jarvis/nodes/{CC_NODE_ID}/settings/request`
         via paho-mqtt at the compose-mapped mosquitto port (127.0.0.1:1883).
      2. Test POSTs `/api/v0/nodes/{CC_NODE_ID}/settings/requests` with
         `Authorization: Bearer <CC_USER_JWT>` — CC creates a
         SettingsRequest row and publishes the MQTT signal synchronously
         inside the handler (see jarvis-command-center/app/node_settings.py:221).
      3. Test waits up to 10s for the message and asserts:
         - `node_id` field matches CC_NODE_ID
         - `request_id` field matches the request_id in the 201 response
           body (proves publish + response stayed consistent)

    Why this matters: the entire server→node side of the voice loop
    runs through MQTT (TTS, settings, package install, factory reset,
    etc.). If the publish path drifts — wrong topic name, wrong
    payload shape, broker URL mis-resolved — every node in the field
    silently misses server messages. CASE-212 is the canary.

    Plumbing notes:
      - The mosquitto port mapping (1883:1883) was added in v2.13 —
        previously the broker was reachable only inside the compose
        network.
      - paho-mqtt was added to the runner's pip install in v2.13.
      - CC_USER_JWT comes from /auth/register's access_token, captured
        by seed.sh (v2.13). The CI user has admin role on their
        auto-created household so verify_household_role passes.
    """
    import json
    import queue
    from urllib.parse import urlparse

    # paho-mqtt is only needed for this case; defer import so collection
    # works on environments without it (locally without the optional dep).
    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    parsed = urlparse(MQTT_BROKER_URL)
    broker_host = parsed.hostname or "127.0.0.1"
    broker_port = parsed.port or 1883

    received: queue.Queue = queue.Queue()
    topic = f"jarvis/nodes/{CC_NODE_ID}/settings/request"

    def on_connect(client, _userdata, _flags, _rc, *_args):
        client.subscribe(topic, qos=1)

    def on_message(_client, _userdata, msg):
        received.put((msg.topic, msg.payload))

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="ci-case-212-subscriber",
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker_host, broker_port, keepalive=30)
    client.loop_start()
    try:
        # Trigger the publish.
        response = httpx.post(
            f"{CC_URL}/api/v0/nodes/{CC_NODE_ID}/settings/requests",
            headers={"Authorization": f"Bearer {CC_USER_JWT}"},
            timeout=15.0,
        )
        assert response.status_code == 201, (
            f"expected 201 from settings/requests, got {response.status_code} "
            f"body={response.text[:400]}"
        )
        expected_request_id = response.json().get("request_id")
        assert expected_request_id, (
            f"expected request_id in response, got {response.json()}"
        )

        try:
            received_topic, raw_payload = received.get(timeout=10.0)
        except queue.Empty:
            raise AssertionError(
                f"timed out waiting 10s for MQTT publish on {topic}. "
                f"Check: mosquitto port 1883 mapped to host? "
                f"CC's JARVIS_MQTT_BROKER_URL points at mosquitto? "
                f"settings/requests handler still calls "
                f"_publish_settings_request_mqtt?"
            )

        assert received_topic == topic, (
            f"unexpected topic, got {received_topic}"
        )
        payload = json.loads(raw_payload.decode("utf-8"))
        assert payload.get("node_id") == CC_NODE_ID, (
            f"expected node_id={CC_NODE_ID}, got payload={payload}"
        )
        assert payload.get("request_id") == expected_request_id, (
            f"expected request_id={expected_request_id} to match the 201 "
            f"response, got payload={payload}"
        )
    finally:
        client.loop_stop()
        client.disconnect()


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-213")
def test_cc_voice_command_mixed_tools_iterates():
    """Mixed server+client tool branch — CC runs the server tool,
    appends its result to the conversation, re-calls the LLM, then
    returns the second-iteration result.

    Code path in CC (`tool_execution_engine.py`):
      ```
      if server_results and client_calls:
          continue  # run server first, then re-ask
      ```

    Sequence:
      1. /voice/command/stream "test mixed tools"
      2. Fake LLM iter 1 → returns [remember (server) + client_tool_three].
         CC's tool exec engine:
           - executes `remember` server tool (returns no_speaker error;
             still counts as server_results populated)
           - sees both server_results AND client_calls → continues loop
           - appends the tool result message to the conversation
      3. Fake LLM iter 2 → matches the second canned entry (gated on
         `requires_tool_message: true`) → returns [client_tool_four]
         only. No server tools this time → CC returns 202 with that.

    Asserts: 202, stop_reason=tool_calls, exactly 1 tool_call, name
    == "client_tool_four". The choice of "client_tool_four" (not
    "client_tool_three") is the proof — if the loop didn't iterate,
    we'd see iter-1's response (which has client_tool_three +
    remember). Getting client_tool_four end-to-end means the
    server-then-loop-continue path actually fired.

    This is the second multi-tool case (CASE-211 was all-client);
    the mixed branch is what CC follows when the LLM wants the
    user/conversation to know a memory/recall happened before the
    client takes an action.
    """
    conv_id = "ci-conv-213"

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
            "voice_command": "test mixed tools",
            "conversation_id": conv_id,
        },
        timeout=30.0,
    )
    assert response.status_code == 202, (
        f"expected 202, got {response.status_code} body={response.text[:400]}"
    )
    body = response.json()
    assert body.get("stop_reason") == "tool_calls", (
        f"expected stop_reason=tool_calls, got body={body}"
    )
    tool_calls = body.get("tool_calls") or []
    names = [tc.get("function", {}).get("name") for tc in tool_calls]
    assert names == ["client_tool_four"], (
        f"expected exactly [client_tool_four] (the iteration-2 response, "
        f"proving CC's mixed-tool loop ran the server tool then re-called "
        f"the LLM), got {names}. If you see ['remember', 'client_tool_three'] "
        f"the loop didn't continue past iter 1; if you see [] CC dropped "
        f"the client_calls when continuing the loop."
    )


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.skipif(not CC_USER_JWT, reason=SKIP_NO_JWT)
@pytest.mark.qa_case("CASE-214")
def test_cc_publishes_to_mqtt_on_factory_reset():
    """Factory-reset is the highest-blast-radius MQTT publish surface.

    Triggered by POST `/api/v0/admin/nodes/{node_id}/factory-reset` (or
    DELETE `/nodes/{node_id}`), it tells the node to wipe local state
    and re-provision. If the topic or payload shape drifts, every prod
    node silently misses factory-reset commands — bricking the mobile
    "reset device" flow with no error surfaced anywhere.

    Sequence:
      1. Test subscribes to `jarvis/nodes/{CC_NODE_ID}/factory-reset`
         via paho-mqtt at 127.0.0.1:1883.
      2. Test POSTs `/api/v0/admin/nodes/{CC_NODE_ID}/factory-reset`
         with `Authorization: Bearer <CC_USER_JWT>`. CC creates a
         `NodeTask(kind="factory_reset")`, mints a reset token, and
         publishes the MQTT signal synchronously (see
         jarvis-command-center/app/admin.py:504-516).
      3. Test asserts the message arrives within 10s and the payload
         fields match the 200 response body:
           - `node_id` == CC_NODE_ID
           - `request_id` == reset_token from the response
           - `task_id` == task_id from the response

    What CASE-214 catches beyond CASE-212:
      - The factory-reset code path (different handler, different
        payload shape with task_id added — CASE-212 only asserted
        node_id + request_id).
      - The MQTT publish is on a *different* topic suffix, so a global
        topic-format change would surface here too.
      - The NodeTask creation flow + reset_token round-trip both work.

    Side-effect note: the POST creates a `NodeTask(state="dispatched")`
    in CC's DB but doesn't actually delete the node from auth. The
    node would normally receive the MQTT and POST back to
    `/nodes/factory-reset/{task_id}/status`; without a real node
    listening, the task sits in `dispatched`. `compose down -v` at
    the end of CI tears down all state, so subsequent runs start
    clean.
    """
    import json
    import queue
    from urllib.parse import urlparse

    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    parsed = urlparse(MQTT_BROKER_URL)
    broker_host = parsed.hostname or "127.0.0.1"
    broker_port = parsed.port or 1883

    received: queue.Queue = queue.Queue()
    topic = f"jarvis/nodes/{CC_NODE_ID}/factory-reset"

    def on_connect(client, _userdata, _flags, _rc, *_args):
        client.subscribe(topic, qos=1)

    def on_message(_client, _userdata, msg):
        received.put((msg.topic, msg.payload))

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="ci-case-214-subscriber",
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker_host, broker_port, keepalive=30)
    client.loop_start()
    try:
        response = httpx.post(
            f"{CC_URL}/api/v0/admin/nodes/{CC_NODE_ID}/factory-reset",
            headers={"Authorization": f"Bearer {CC_USER_JWT}"},
            timeout=15.0,
        )
        assert response.status_code == 200, (
            f"expected 200 from factory-reset, got {response.status_code} "
            f"body={response.text[:400]}"
        )
        body_json = response.json()
        expected_reset_token = body_json.get("reset_token")
        expected_task_id = body_json.get("task_id")
        assert expected_reset_token, (
            f"expected reset_token in response, got {body_json}"
        )
        assert expected_task_id, (
            f"expected task_id in response, got {body_json}"
        )

        try:
            received_topic, raw_payload = received.get(timeout=10.0)
        except queue.Empty:
            raise AssertionError(
                f"timed out waiting 10s for MQTT publish on {topic}. "
                f"Check: did CC create the NodeTask but fail to publish? "
                f"(admin.py:504-516 try/except swallows publish errors)"
            )

        assert received_topic == topic, (
            f"unexpected topic, got {received_topic}"
        )
        payload = json.loads(raw_payload.decode("utf-8"))
        assert payload.get("node_id") == CC_NODE_ID, (
            f"expected node_id={CC_NODE_ID}, got payload={payload}"
        )
        assert payload.get("request_id") == expected_reset_token, (
            f"expected request_id={expected_reset_token} to match "
            f"reset_token in response, got payload={payload}"
        )
        assert payload.get("task_id") == expected_task_id, (
            f"expected task_id={expected_task_id} to match response, "
            f"got payload={payload}"
        )
    finally:
        client.loop_stop()
        client.disconnect()


@pytest.mark.skipif(not CC_URL, reason=SKIP_REASON)
@pytest.mark.skipif(
    not (CC_NODE_ID and CC_NODE_KEY), reason=SKIP_NO_NODE
)
@pytest.mark.qa_case("CASE-215")
def test_cc_publishes_to_mqtt_on_package_install():
    """Package-install MQTT — the Pantry integration channel.

    Every dynamic command install on a node flows through this topic.
    Mobile picks a Pantry package → POSTs CC's package-install endpoint
    → CC stores the request + publishes `jarvis/nodes/{id}/package-install`
    → node receives MQTT → clones the repo at the requested git_tag →
    POSTs results back to CC. Topic shape drift = the entire Pantry
    install flow silently fails.

    Distinct from CASE-212/214 in two ways:
      - Auth: `verify_provisioning_auth` accepts EITHER the admin key
        (X-API-Key) or a user JWT. We use the admin key here — simpler
        than the JWT path and exercises that branch of the verifier.
      - Payload is richer: `{request_id, command_name, github_repo_url,
        git_tag}`. CASE-214 round-tripped 3 fields; this round-trips 4.

    Sequence:
      1. Subscribe to `jarvis/nodes/{CC_NODE_ID}/package-install`.
      2. POST `/api/v0/nodes/{CC_NODE_ID}/package-install` with
         `X-API-Key: ci-admin-key` and a known package body.
      3. Assert 201 + body.id present + the MQTT payload's
         request_id matches body.id, and command_name /
         github_repo_url / git_tag echo what we sent.

    Side-effect note: creates a `PackageInstallRequest` row with
    status="pending" and an expires_at 5 minutes out. With no real node
    to follow up, the row sits pending until expiry — `compose down -v`
    clears it between CI runs.
    """
    import json
    import queue
    from urllib.parse import urlparse

    import paho.mqtt.client as mqtt
    from paho.mqtt.enums import CallbackAPIVersion

    parsed = urlparse(MQTT_BROKER_URL)
    broker_host = parsed.hostname or "127.0.0.1"
    broker_port = parsed.port or 1883

    received: queue.Queue = queue.Queue()
    topic = f"jarvis/nodes/{CC_NODE_ID}/package-install"

    def on_connect(client, _userdata, _flags, _rc, *_args):
        client.subscribe(topic, qos=1)

    def on_message(_client, _userdata, msg):
        received.put((msg.topic, msg.payload))

    client = mqtt.Client(
        callback_api_version=CallbackAPIVersion.VERSION2,
        client_id="ci-case-215-subscriber",
    )
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(broker_host, broker_port, keepalive=30)
    client.loop_start()
    try:
        # Known payload — the values flow through CC unchanged to the
        # MQTT message, so we can assert each field round-tripped.
        request_body = {
            "command_name": "ci-test-package",
            "github_repo_url": "https://github.com/example/ci-test-package",
            "git_tag": "v0.0.1",
        }
        response = httpx.post(
            f"{CC_URL}/api/v0/nodes/{CC_NODE_ID}/package-install",
            headers={"X-API-Key": "ci-admin-key"},
            json=request_body,
            timeout=15.0,
        )
        assert response.status_code == 201, (
            f"expected 201 from package-install, got {response.status_code} "
            f"body={response.text[:400]}"
        )
        body_json = response.json()
        expected_request_id = body_json.get("id")
        assert expected_request_id, (
            f"expected id in response, got {body_json}"
        )

        try:
            received_topic, raw_payload = received.get(timeout=10.0)
        except queue.Empty:
            raise AssertionError(
                f"timed out waiting 10s for MQTT publish on {topic}. "
                f"Check: did CC store the row but fail to publish? "
                f"(package_install.py:541-565 try/except swallows errors)"
            )

        assert received_topic == topic, (
            f"unexpected topic, got {received_topic}"
        )
        payload = json.loads(raw_payload.decode("utf-8"))
        assert payload.get("request_id") == expected_request_id, (
            f"expected request_id={expected_request_id} to match "
            f"response.id, got payload={payload}"
        )
        # Every field in request_body should survive verbatim into the
        # MQTT payload.
        for field in ("command_name", "github_repo_url", "git_tag"):
            assert payload.get(field) == request_body[field], (
                f"expected {field}={request_body[field]} to round-trip, "
                f"got payload[{field}]={payload.get(field)!r}"
            )
    finally:
        client.loop_stop()
        client.disconnect()
