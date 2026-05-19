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
