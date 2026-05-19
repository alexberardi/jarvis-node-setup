# QA Integration Test Execution Layer

A GitHub-Actions-based CI loop that closes the post-PR test-execution gap in
the openclaw agentic workflow. When a coding-agent (or human) opens a PR in
a participating service repo, this layer brings up a real Jarvis service
stack against the PR's source, runs a marker-bound pytest suite, and posts
a structured result comment plus a commit status back on the PR — without
granting the QA agent any runtime or write permissions.

**Current state: v2.3 — 8/8 cases passing.** The QA agent on the Pi stays
read-only by design. CI does the work; QA reads the result comment (next
phase) and posts a `<!-- qa-execution-report:v1 -->` sentinel back on the
originating roadmap issue.

---

## Mental model

Two GitHub repos, one runner, two scoped PATs. A docker-compose stack
brought up in two phases with an admin-API seed step between them so CC
gets started with auth-generated credentials:

```
┌────────────────────────────────────────────┐         ┌─────────────────────────────────────────────────────────────────────┐
│ Service repo (e.g. jarvis-command-center)  │         │ jarvis-node-setup (the integration runner)                          │
│                                             │         │                                                                       │
│  .github/workflows/                         │         │  .github/workflows/integration-runner.yml                            │
│    integration-trigger.yml                  │         │                                                                       │
│                                             │         │  on: repository_dispatch                                              │
│  on: pull_request                           │         │      types: [pr-integration]                                          │
│      ↓                                       │  POST    │      ↓                                                                │
│  gh api .../dispatches  ───────────────────▶│ ──────▶ │  1. checkout jarvis-node-setup                                       │
│      (INTEGRATION_DISPATCH_TOKEN)           │         │  2. checkout originating service @ head_sha into _src/<svc>          │
│                                             │         │  3. setup buildx + ghcr login                                         │
│                                             │         │  4. start fakes (fake_llm + fake_whisper, bg processes)              │
│                                             │         │  5. Phase 1: compose up postgres + auth + config-service + mqtt     │
│                                             │         │  6. seed.sh:                                                          │
│                                             │         │       POST auth /admin/app-clients → capture auth-generated keys    │
│                                             │         │       POST auth /auth/register → capture CC_HOUSEHOLD_ID            │
│                                             │         │       export JARVIS_CC_APP_KEY + CC_HOUSEHOLD_ID to $GITHUB_ENV     │
│                                             │         │  7. Phase 2: compose up jarvis-command-center                        │
│                                             │         │       (built from PR source, env interpolates JARVIS_CC_APP_KEY)    │
│                                             │         │  8. Phase 2.5: POST CC /admin/nodes (CC registers in auth + writes  │
│                                             │         │       its own DB row); capture CC_NODE_KEY                          │
│                                             │         │  9. pytest tests/test_loop_smoke.py tests/test_cc_real_smoke.py     │
│                                             │         │ 10. parse_junit → JSON map                                            │
│                                             │  POST    │ 11. render comment + post                                            │
│  PR comment ◀──────────────────────────────│ ◀────── │      (INTEGRATION_COMMENT_TOKEN)                                      │
│  + jarvis-integration commit status         │         │                                                                       │
└────────────────────────────────────────────┘         └─────────────────────────────────────────────────────────────────────┘
```

The runner is a single point of change. Each service repo only needs the
~50-line trigger workflow — it knows nothing about *how* tests run.

---

## Status snapshot

| Increment | What it adds | Cases |
|---|---|---|
| **v1** | Dispatch + receiver loop with fakes-only smoke. Plumbing proven. | `CASE-001`, `CASE-002`, `CASE-003` |
| **v2.1** | docker-compose.ci.yaml + CC built from PR source against Postgres + cross-repo checkout. | `CASE-101` (CC /health), `CASE-102` (CC / non-5xx) |
| **v2.2** | Real `jarvis-auth` + `jarvis-config-service` from ghcr.io `:dev` + Docker Buildx GHA layer caching. | `CASE-103` (config-svc /health), `CASE-104` (auth /health) |
| **v2.3** | Two-phase compose with `seed.sh` between: registers CC's app-client in auth (auth generates the key), passes the key into CC's compose env, verifies the seeded credentials work. | `CASE-201` (seeded creds authenticate against auth) |
| **v2.4** | User-signup → household chain added to `seed.sh`. Node registration originally went directly to auth's `/admin/nodes`; superseded in v2.5 by CC's `/admin/nodes` (which does both auth + local DB in one shot). | `CASE-202` (real seeded node validates `valid=true` against auth) |
| **v2.5** | First node-authenticated CC endpoint: `POST /api/v0/conversation/start` with `X-API-Key: <node_id>:<node_key>`. Phase 2.5 step calls CC's `/admin/nodes` (CC registers in auth via `/internal/nodes/register` AND writes its own DB row — both rows required for `verify_api_key` to pass). | `CASE-203` (CC accepts seeded node creds end-to-end) |
| **v2.6** | First end-to-end voice command exercise: `POST /api/v0/voice/command/stream` with a tool-eliciting prompt. Round-trip goes CC → fake LLM (canned tool_calls response) → back to CC → 202 JSON to the test. Proves the LLM-proxy URL fix from v2.5 actually unblocks real LLM traffic. | `CASE-204` (timer prompt → 202 with `set_timer` tool_call) |
| **v2.7** | Tool-execution continuation loop: after the CASE-204 tool call comes back, POST tool results to `/voice/command/continue` (blocking) and assert the final assistant message. Exercises CC's continuation prompt build ("Here are the tool results...") + a second LLM call + JSON response shape. | `CASE-205` (continue with `tool_results` → 200 with `stop_reason=complete`, `assistant_message` contains 'timer') |
| **v2.8** | End-to-end audio path: `/voice/command/continue/stream` from tool result to PCM bytes. Adds `tests/fakes/fake_tts.py` at port 7707 (mirrors jarvis-tts's `/speak/stream` + `/audio/format`) and SSE support in `fake_llm_backend.py`. CC's pipeline streams SSE tokens → sentence boundary → fake TTS → audio bytes back to the test. | `CASE-206` (continue/stream → 200 audio/raw with non-zero bytes + X-Audio-* headers) |
| **v2.9** | Upstream voice-loop edges: wake-acknowledge + STT media proxy. Also fixes a latent fake_whisper field-name bug (`audio` → `file`, matching the real jarvis-whisper-api). | `CASE-207` (acknowledge → 200 JSON `{text}`), `CASE-208` (media/whisper/transcribe → canned timer transcript) |
| **v2.10** | Symmetric pair to CASE-204: `/voice/command/stream`'s 200 audio branch when the LLM returns a plain conversational reply (no tool_calls). Closes the last `/voice/command/*` branch — combined with 204/205/206, every voice endpoint has end-to-end coverage. | `CASE-209` ("hello jarvis" → 200 audio/raw with PCM bytes from the TTS roundtrip) |
| **v2.11** | `validation_required` branch — when the LLM emits the `request_validation` server tool because a parameter is ambiguous, CC translates that to a 202 with `stop_reason=validation_required` + a `validation_request` body. Exercises the server-tool execution path (previously only client tools were covered). | `CASE-210` ("play music" → 202 with question "Which artist?") |
| **v2.12** | Multi-tool flow — LLM emits 2 tool_calls in one response. CC's tool exec engine returns both in a single 202 with their IDs/order intact. Catches drift in the `server_results` + `client_calls` split logic. | `CASE-211` ("test multi-tool flow" → 202 with two client tool_calls in order) |
| **v2.13** *(next)* | Fan `integration-trigger.yml` out to remaining ~15 service repos. | (per-service cases) |

Round-trip on a coding-agent PR: ~3-4 min cold, ~1 min warm once Buildx
GHA cache primes.

---

## End-to-end flow

For one PR in `jarvis-command-center`:

1. **PR opens / synchronizes / reopens** against `main`.
2. **`integration-trigger.yml` fires** on the `pull_request` event. It POSTs
   to `/repos/alexberardi/jarvis-node-setup/dispatches` with:

   ```json
   {
     "event_type": "pr-integration",
     "client_payload": {
       "service": "jarvis-command-center",
       "pr_number": "<number>",
       "head_sha": "<sha>",
       "head_ref": "<branch>",
       "originating_repo": "alexberardi/jarvis-command-center",
       "qa_plan_comment_id": "",
       "linked_prs": "{}"
     }
   }
   ```

   Auth: `INTEGRATION_DISPATCH_TOKEN` secret in the originating repo.
3. **`integration-runner.yml`** in `jarvis-node-setup` is listening for
   that dispatch type *on its default branch* (`repository_dispatch` only
   fires workflows from the default branch — that's why the runner has to
   be on `main` before anything routes to it).
4. **Resolve the payload** into step outputs (`service`, `pr_number`,
   `head_sha`, `originating_repo`, `plan_cases`, `linked_prs`,
   `bring_up_cc`). `bring_up_cc=true` iff
   `service == "jarvis-command-center"`.
5. **Set up Python 3.11** + cache pip + install deps:
   `pytest pytest-asyncio fastapi uvicorn httpx pydantic pyyaml python-multipart`.
6. **Check out originating service repo at PR head**
   (`actions/checkout@v4`) into `_src/jarvis-command-center` *inside* the
   workspace. (Paths outside the workspace are rejected by
   actions/checkout — that's why the local-dev default in compose is
   `../jarvis-command-center` but CI overrides via `CC_SOURCE_PATH`.)
7. **Set up Docker Buildx** + **log in to GHCR** using the workflow's
   `GITHUB_TOKEN` with `permissions: packages: read`. The login is what
   lets the runner pull the private `ghcr.io/alexberardi/jarvis-auth:dev`
   and `:config-service:dev` images.
8. **Start the fakes as background processes** — `fake_llm_backend.py`
   on 7705, `fake_whisper.py` on 7706. Both load
   `tests/fakes/canned_responses.yaml` and run as plain Python processes
   on the runner host; containerized services reach them via
   `host.docker.internal`.
9. **Wait for fakes** `/health`. 20s timeout; on timeout, dump fake logs.
10. **Phase 1 — bring up the deps stack:**

    ```bash
    docker compose -f docker-compose.ci.yaml --profile core up -d --wait \
      postgres jarvis-auth jarvis-config-service mosquitto
    ```

    Notable behaviors:
    - Postgres mounts `compose/postgres-init.sh`, which on first init
      runs `CREATE DATABASE jarvis_auth; CREATE DATABASE jarvis_config;`
      so the other services have somewhere to migrate to.
    - Postgres image is `pgvector/pgvector:pg15` because CC's alembic
      migrations issue `CREATE EXTENSION IF NOT EXISTS vector`.
    - auth's image CMD chains `alembic upgrade head` + uvicorn, so it
      self-migrates on boot.
    - config-service's image CMD doesn't migrate, so compose overrides
      `command:` to chain alembic + uvicorn.
11. **Seed step** — `bash compose/seed.sh`:
    1. Pre-flight `/health` probe of auth + config-service.
    2. `POST auth /admin/app-clients` to register `command-center` and
       `jarvis-config-service` app-clients. Auth generates the keys and
       returns them; we capture both.
    3. `POST config-service /services` to register the fakes:
       - `jarvis-llm-proxy-api` → `host.docker.internal:7705`
       - `jarvis-whisper-api` → `host.docker.internal:7706`
       (Belt-and-suspenders alongside CC's `JARVIS_LLM_PROXY_URL` env
       fallback — registration failures here log a WARN but don't fail
       the seed.)
    4. `POST auth /auth/register` for the CI user. The endpoint
       auto-creates a default household; we capture `household_id`.
    5. Write `CC_APP_KEY`, `JARVIS_CC_APP_KEY`, `CFG_APP_KEY`, and
       `CC_HOUSEHOLD_ID` to `$GITHUB_ENV`. (Node-registration happens
       *after* CC is up — see Phase 2.5 below.)
12. **Phase 2 — bring up CC with the seeded app-key:**

    ```bash
    JARVIS_CC_APP_KEY=$JARVIS_CC_APP_KEY docker compose -f docker-compose.ci.yaml \
      --profile core up -d --wait jarvis-command-center
    ```

    CC's compose env reads `${JARVIS_CC_APP_KEY:-ci-app-key}`, so the
    shell-set value flows in. CC's CMD chains `alembic upgrade head` +
    uvicorn; its healthcheck hits `/health` from inside the container.
13. **Phase 2.5 — register node in CC** (inline curl step):

    ```bash
    POST $CC_URL/api/v0/admin/nodes
      X-API-Key: $ADMIN_API_KEY   (== ci-admin-key, from docker-compose.ci.yaml)
      body: {node_id: ci-node-001, household_id: <CC_HOUSEHOLD_ID>,
             room: ci-room, name: CI Node}
    ```

    CC's endpoint does **two things in one shot**:
    - POSTs auth's `/internal/nodes/register` (CC's app credentials
      auto-grant `command-center` service access — see
      `jarvis-auth/jarvis_auth/app/api/internal.py:106`).
    - Inserts a row into CC's local `nodes` table (the row CC's
      `verify_api_key` looks up after auth says valid — see
      `jarvis-command-center/app/deps.py:145-148`).

    Both rows have to exist for CC to accept `X-API-Key: node_id:node_key`.
    Calling CC's endpoint unifies the two registrations.

    Captures `node_key` from the response and exports `CC_NODE_ID` +
    `CC_NODE_KEY` to `$GITHUB_ENV`.
14. **Run pytest:**

    ```bash
    FAKE_LLM_URL=http://127.0.0.1:7705 \
    FAKE_WHISPER_URL=http://127.0.0.1:7706 \
    CC_URL=http://localhost:7703 \
    AUTH_URL=http://localhost:7701 \
    CONFIG_URL=http://localhost:7700 \
    CC_APP_ID=command-center \
    CC_APP_KEY=$CC_APP_KEY \
    CC_NODE_ID=$CC_NODE_ID \
    CC_NODE_KEY=$CC_NODE_KEY \
    pytest tests/test_loop_smoke.py tests/test_cc_real_smoke.py \
        --junit-xml=results.xml -v
    ```

    Each test has `@pytest.mark.qa_case("CASE-NNN")`. The hook in
    `tests/conftest.py` copies that marker into `item.user_properties`,
    which pytest then serializes into the XML as
    `<property name="qa_case" value="CASE-001"/>`. The step has
    `continue-on-error: true` so the workflow keeps going even if tests
    fail — we want results posted either way.
15. **Parse the XML, render comment, post comment, post commit status**
    — same as v1. See "Component reference" below for shape details.
16. **Cleanup** — `compose down -v --remove-orphans`, kill fake PIDs,
    dump fake stdout logs to the run log.

---

## Component reference

### `.github/workflows/integration-trigger.yml` (each participating service repo)

Tiny — just fans the `pull_request` event out to a `repository_dispatch`.
Lives in every repo we want covered. Today: `jarvis-command-center`
only.

| | |
|---|---|
| Trigger | `pull_request: [opened, synchronize, reopened]` targeting `main` |
| Auth | `INTEGRATION_DISPATCH_TOKEN` secret in this repo |
| Target | `/repos/alexberardi/jarvis-node-setup/dispatches` |
| Payload | `{service, pr_number, head_sha, head_ref, originating_repo, qa_plan_comment_id, linked_prs}` |
| Timeout | 2 min (it's just an API call) |

The workflow is the *only* place to set `service` per repo — that field
is how the receiver knows which service the PR belongs to.

### `.github/workflows/integration-runner.yml` (jarvis-node-setup)

The receiver and orchestrator. Lives in `jarvis-node-setup` because that
repo already plays the role of "central place for cross-service tests."

| | |
|---|---|
| Trigger | `repository_dispatch: types: [pr-integration]` + `workflow_dispatch` |
| Manual inputs | `service`, `pr_number`, `head_sha`, `originating_repo`, `qa_plan_comment_id`, `plan_cases`, `linked_prs` |
| Concurrency group | `integration-<originating_repo>-<pr_number>` with `cancel-in-progress: true` |
| Timeout | 15 min (hard cap; in practice ~3-4 min cold) |
| Permissions | `contents: read` + `packages: read` (for ghcr pulls) |
| Required secret | `INTEGRATION_COMMENT_TOKEN` (this repo) |

### `docker-compose.ci.yaml`

The CI stack definition. Brings up:

| Service | Image / Build | Internal port | Host port | Notes |
|---|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg15` | 5432 | (not exposed) | pgvector needed for CC's memory embeddings. Init script creates `jarvis_auth` + `jarvis_config` DBs. |
| `mosquitto` | `eclipse-mosquitto:2` | 1883 | (not exposed) | MQTT broker for CC's async server→node channel. |
| `jarvis-auth` | `ghcr.io/alexberardi/jarvis-auth:dev` | 8000 | 7701 | Image CMD auto-runs alembic. `JARVIS_AUTH_ADMIN_TOKEN=ci-auth-admin-token`. |
| `jarvis-config-service` | `ghcr.io/alexberardi/jarvis-config-service:dev` | 7700 | 7700 | Compose `command:` override chains alembic + uvicorn since the image CMD doesn't migrate. |
| `jarvis-command-center` | Built from `${CC_SOURCE_PATH:-../jarvis-command-center}` | 8002 | 7703 | Buildx GHA cache enabled. Compose `command:` chains alembic + uvicorn. `JARVIS_APP_KEY: ${JARVIS_CC_APP_KEY:-ci-app-key}` — set by seed.sh. |

Profiles: everything except `postgres` is under `profiles: ["core"]`.

### `compose/postgres-init.sh`

Runs once when the postgres container is first initialized (mounted at
`/docker-entrypoint-initdb.d/01-create-dbs.sh`). Creates the
`jarvis_auth` and `jarvis_config` databases. pgvector extension is
created by CC's own migrations on `jarvis_command_center` only.

### `compose/seed.sh`

Runs between phase 1 and phase 2. Reads:

| Env var | Default | Purpose |
|---|---|---|
| `AUTH_URL` | `http://localhost:7701` | The host-mapped auth port |
| `CONFIG_URL` | `http://localhost:7700` | The host-mapped config-service port |
| `AUTH_ADMIN_TOKEN` | `ci-auth-admin-token` | Sent as `X-Jarvis-Admin-Token` to auth's admin APIs |
| `CONFIG_ADMIN_TOKEN` | `ci-auth-admin-token` | Sent as `X-Admin-Token` to config-service's admin APIs |
| `GITHUB_ENV` | — | If set, captured values are written here for downstream workflow steps |

Writes:

| Env name | Value | Used by |
|---|---|---|
| `CC_APP_KEY` | The key auth generated for `command-center` | The pytest step (`CASE-201`, `CASE-202`) |
| `JARVIS_CC_APP_KEY` | Same value | The compose `up jarvis-command-center` step (interpolated into CC's env) |
| `CFG_APP_KEY` | The key auth generated for `jarvis-config-service` | (currently unused, captured for future v2.13+ use) |
| `CC_HOUSEHOLD_ID` | The household auto-created by `/auth/register` for the CI user | The Phase 2.5 step (consumed by `POST CC /admin/nodes` to attach the node to a household) |

`CC_NODE_ID` and `CC_NODE_KEY` are *not* written here — they're set by the **Phase 2.5 workflow step** (`Register node in CC`) that runs after CC is up. seed.sh runs before CC is up, so the node registration has to happen later.

Key design points:
- `log()` writes to **stderr** (not stdout) so it doesn't pollute the
  `$(register_app_client ...)` capture. The first v2.3 run failed
  exactly because log lines were getting captured into `$CC_RESPONSE`,
  making the JSON parse fail.
- `http_post()` helper uses `curl -sS -o <file> -w '%{http_code}'` so
  it captures both the HTTP status and the response body. On non-2xx it
  dumps both to stderr — the CI log makes any future seed failure
  obvious instead of a useless `JSONDecodeError`.
- Fakes registration is best-effort (`|| log WARN`); CC has env-var
  fallbacks for those URLs so a 409 (duplicate) or similar doesn't fail
  the whole seed.

### `tests/fakes/fake_llm_backend.py`

FastAPI shim mimicking `jarvis-llm-proxy-api`. Endpoint:
`POST /v1/chat/completions` (matches the real proxy's OpenAI-style
route). Reads `canned_responses.yaml`, regex-matches the latest user-role
message body (first match wins), and translates the canned entry to
OpenAI shape.

**Two emission paths, both real:**

- *Plain-text* — canned has `content: "..."` with no `tool_calls`. The
  fake emits `choices[0].message.content = "..."` and `finish_reason
  = "stop"`. CC's text-based parser tries to JSON-decode the content,
  fails, falls back to a plain-string assistant message. Matches what
  the real proxy does for non-tool replies.
- *Tool-call* — canned has a `tool_calls: [...]` array. The fake
  emits the tool calls as a JSON string in `message.content`:
  `{"message": "<canned content>", "tool_calls": [{"name": ..., "arguments": {...}}]}`,
  with `finish_reason = "stop"`. This matches what adapter-trained
  models do in prod — they emit JSON in content (LoRA-trained on this
  exact shape) and the proxy returns it verbatim. CC's
  `tool_call_parser.parse_response` JSON-decodes the content and
  pulls `tool_calls` from it.

**Why content-as-JSON and not native `message.tool_calls`?** CC's
`use_native_tools` is False unless a prompt provider for the current
model class is registered in `app/core/prompt_providers/`. The default
`JarvisAdapterModel` doesn't have one in stock — CC logs
`"PromptProviderFactory: 'JarvisAdapterModel' not found in prompt_providers"`
and falls through to the text-based parser. The real adapter-trained
models bypass native tool-calling entirely and emit JSON content; the
fake matches that.

**Streaming (v2.8+):** When the request body has `stream: true`, the
fake emits an SSE response — one `data: {"delta": "<word> "}` event per
space-delimited word in the canned content, followed by `data: {"done":
true, "content": "<full text>"}`. The word-by-word chunking is what
makes CC's sentence-boundary detector (`(?<=[.!?])\s+`) actually trigger
for canned responses like `"Timer set for 5 minutes."` — a single
mega-delta would never split, and TTS would never be invoked.

Unmatched prompts fall back to `content: "OK"`, `finish_reason: stop`.
Bound to `0.0.0.0` so CC containers can reach the fake via
`host.docker.internal` (loopback-only would only be reachable from the
runner host process).

```bash
python -m tests.fakes.fake_llm_backend --port 7705 \
    --responses tests/fakes/canned_responses.yaml
```

Env overrides: `FAKE_LLM_PORT`, `FAKE_LLM_RESPONSES`.

### `tests/fakes/fake_tts.py`

FastAPI shim mimicking `jarvis-tts`. Endpoints:

- `POST /speak/stream` — accepts `{"text": "..."}` and returns
  `audio/raw` with 32 bytes of zero PCM plus the audio-format
  headers (`X-Audio-Sample-Rate: 22050`, `X-Audio-Channels: 1`,
  `X-Audio-Sample-Width: 2`). CC's `tts_client.speak_stream` reads
  the chunks via `aiter_bytes`; one yielded chunk is enough to prove
  the wire works.
- `GET /audio/format` — returns the same format metadata as JSON;
  CC calls this once before opening the audio stream.
- `GET /health` — required by the workflow's fakes-health-check
  loop.

The real `jarvis-tts` requires app-to-app auth (`X-Jarvis-App-Id` +
`X-Jarvis-App-Key`). The fake doesn't validate — auth is already
covered by CASE-201/202. Bound to `0.0.0.0` like the other fakes.

```bash
python -m tests.fakes.fake_tts --port 7707
```

Env overrides: `FAKE_TTS_PORT`.

### `tests/fakes/fake_whisper.py`

FastAPI shim mimicking `jarvis-whisper-api`. Endpoint: `POST /transcribe`
(multipart). Regex-matches the uploaded audio filename against
`canned_responses.yaml` `transcripts` entries; unmatched filenames return
`"fake transcript"`.

```bash
python -m tests.fakes.fake_whisper --port 7706
```

Env overrides: `FAKE_WHISPER_PORT`, `FAKE_WHISPER_RESPONSES`.

**Note**: this shim uses `UploadFile`, so FastAPI requires
`python-multipart` at import time. The runner's pip install includes it
explicitly — leaving it out causes the fake to fail to start and every
case to report `not-implemented`.

### `tests/fakes/canned_responses.yaml`

Single file feeding both fakes. Two top-level keys: `responses` for the
LLM shim, `transcripts` for the Whisper shim. See the existing entries
for the format.

### `tests/conftest.py` — the `qa_case` marker hook

```python
def pytest_collection_modifyitems(items):
    for item in items:
        for marker in item.iter_markers(name="qa_case"):
            case_id = marker.args[0] if marker.args else None
            if case_id:
                item.user_properties.append(("qa_case", case_id))
```

Pytest's JUnit serializer turns `user_properties` into
`<property name="qa_case" value="CASE-NNN"/>` elements, which
`parse_junit.py` keys on.

### `tests/test_loop_smoke.py` — v1 fakes-only tests (CASE-001…003)

Three smoke cases that exercise both fakes via `httpx`. Lives at
`tests/` (not `tests/integration/`) because `tests/integration/conftest.py`
imports the production codebase, which depends on `jarvis_command_sdk`.

### `tests/test_cc_real_smoke.py` — v2.1+ real-stack tests (CASE-101…104, 201…211)

All gated by `@pytest.mark.skipif(not CC_URL, ...)` so they cleanly skip
when the compose stack isn't up (v1 fakes-only mode). CASE-201 also
gates on `CC_APP_KEY`; CASE-202…211 additionally gate on `CC_NODE_ID` +
`CC_NODE_KEY` (set by the Phase 2.5 workflow step, not by seed.sh).

| Case | Asserts |
|---|---|
| `CASE-101` | `GET CC /health` returns 200 with `{status: healthy}` |
| `CASE-102` | `GET CC /` returns non-5xx (confirms uvicorn serving) |
| `CASE-103` | `GET config-service /health` returns 200 with `{status: ok}` |
| `CASE-104` | `GET auth /health` returns 200 with `{status: ok}` |
| `CASE-201` | `POST auth /internal/validate-node` with bogus node + CC's seeded app credentials → 200 with `valid: false`. (Auth has no `/internal/validate-app`; app credentials are checked inline on every protected endpoint. A 401 here means app-auth failed; `valid: false` for a nonexistent node means app-auth succeeded.) |
| `CASE-202` | Positive-path counterpart to CASE-201. `POST auth /internal/validate-node` with the real seeded `node_id` + `node_key` + CC's app credentials → 200 with `valid: true`, returned `node_id` matches what we sent, and `household_id` is populated. Together with CASE-201 this nails down both branches of the validate-node contract. |
| `CASE-203` | First end-to-end test through CC. `POST CC /api/v0/conversation/start` with `X-API-Key: <node_id>:<node_key>` and `{conversation_id: "ci-conv-203"}` → 200 with `status: success` and the same `conversation_id` echoed back. Exercises CC's `verify_api_key` → auth's `/internal/validate-node` → CC's local-DB node lookup → CC issues the session. If any of those three steps drift, CASE-203 catches it. |
| `CASE-204` | First voice-command exercise through the LLM. Setup: `POST /conversation/start` with `client_tools: []` (required — `/voice/command/stream` 400s if the conversation cache entry's `tools` field is None). Action: `POST /voice/command/stream` with `voice_command: "set a 5 minute timer"`. The fake LLM regex-matches "set …timer" → returns canned `stop_reason: tool_calls` with a `set_timer` function call. CC's main.py:974+ picks 202 JSON for any non-`complete` stop_reason. Asserts 202, `stop_reason == "tool_calls"`, exactly one tool call, function name is `set_timer`. Proves CC reaches the fake LLM at `host.docker.internal:7705` (the JARVIS_LLM_PROXY_API_URL fix from v2.5's hotfix) and parses the response into VoiceCommandResponse correctly. |
| `CASE-205` | Tool-execution continuation. Two-step: (1) repeat CASE-204's `/voice/command/stream` to get back a 202 with `tool_calls[0].id`; (2) POST `/voice/command/continue` (the BLOCKING JSON endpoint, not the streaming twin) with `{conversation_id, tool_results: [{tool_call_id, output}]}`. CC builds a continuation prompt "Here are the tool results..." and re-calls the fake LLM. The fake matches that regex → returns canned `complete` content "Timer set for 5 minutes." CC's parser fails to JSON-decode the plain text and falls back to ("stop", [], content), producing a 200 JSON VoiceCommandResponse with `stop_reason: complete` and `assistant_message: "Timer set for 5 minutes."`. Asserts 200, `stop_reason == "complete"`, non-empty `assistant_message` containing "timer". Proves the conversation cache + continuation prompt + second LLM call + tool_results body shape all work end-to-end. |
| `CASE-206` | End-to-end audio path. Same setup as CASE-205, but POSTs `/voice/command/continue/stream` instead. CC opens an SSE stream to the fake LLM (`stream=true` in the request body), accumulates tokens to sentence boundaries, and forwards each completed sentence to the fake TTS's `/speak/stream`. The fake TTS returns 32 bytes of zero PCM + `X-Audio-*` headers. CC concatenates the chunks into its own StreamingResponse and forwards them to us. Asserts 200, content-type `audio/raw`, non-zero body, `X-Audio-Sample-Rate` header present. Proves the full audio pipeline — SSE streaming, sentence detection, TTS roundtrip, audio forwarding — works end-to-end against the fakes. |
| `CASE-207` | Wake-acknowledge path. POSTs `/voice/acknowledge` with `{voice_command: "..."}`. CC's `generate_acknowledgment` uses pure regex + curated phrase pools — no LLM, no TTS. Asserts 200 + non-empty `text` field. The keyword pools are randomized so the exact string isn't pinned; the test catches anyone accidentally wiring an LLM call into this hot path (a fakes-only response should complete in well under 100ms — CC's `voice/command/stream` ack runs in parallel with the LLM path and bakes ~50ms of perceived latency into the loop). |
| `CASE-208` | STT media proxy. POSTs `/api/v0/media/whisper/transcribe` as multipart with field `file` and a `timer_clip.wav` filename. CC forwards to the fake whisper at port 7706 (which regex-matches the filename → returns the canned "Set a five minute timer" transcript). Asserts 200 + `text == "Set a five minute timer"`. Proves CC's media proxy plumbing: WhisperClient setup with context headers (X-Household-ID + X-Node-ID + X-Member-IDs), the multipart `file` field name end-to-end (both ends MUST agree — the fix in v2.9 also corrected a latent bug where CASE-003 worked only because the fake and CASE-003 were both wrong with `audio`), and that CC forwards the whisper response unchanged. |
| `CASE-209` | Symmetric pair to CASE-204. POSTs `/voice/command/stream` with `voice_command="hello jarvis"`. The fake LLM regex-matches `\b(hello\|hi\|hey)\b` → returns plain-text content "Hello! How can I help?" with `stop_reason: complete`. CC's `tool_call_parser` fails to JSON-decode the content, falls back to `("stop", [], content)`. `handle_voice_stream` sees `stop_reason == "complete"` + a non-empty assistant_message → takes the 200 audio path: TTSClient → `stream_text_as_audio` → fake TTS roundtrip → PCM bytes back. Asserts 200, content-type audio/raw, non-zero body, X-Audio-Sample-Rate header. With CASE-204/205/206/207/208/209, every `/voice/*` branch is covered. |
| `CASE-210` | The `validation_required` branch. POSTs `/voice/command/stream` with `voice_command="play music"`. The fake LLM matches the new "play music" regex → returns a `request_validation` tool_call with arguments `{question: "Which artist would you like?", parameter_name: "artist", options: [...]}`. CC's tool exec engine recognizes `request_validation` as a *server* tool (not client), executes it locally (the tool returns `{_validation_request: True, question, parameter_name, options}`), detects the marker, and converts it to a 202 with `stop_reason: "validation_required"` + a `validation_request` body. Asserts 202, stop_reason, question contains "artist", parameter_name=="artist", options is a list. This is the first case that exercises the server-tool execution path — every other voice case fired client tool_calls only. |
| `CASE-211` | Multi-tool flow. POSTs `/voice/command/stream` with `voice_command="test multi-tool flow"`. The fake LLM emits two tool_calls in one response (`client_tool_one` + `client_tool_two`, generic names that don't collide with CC's server-tool registry). CC's tool exec engine puts both in `client_calls` and returns a single 202 with both tool_calls in order. Asserts 202, stop_reason=tool_calls, exactly two tool_calls, names in order, distinct IDs. Catches drift in the `server_results` + `client_calls` split (the mixed branch — server-then-client — is a future case). |

### `tools/parse_junit.py`

Stdlib-only (`xml.etree.ElementTree`). Walks every `<testcase>`, looks
for a `qa_case` `<property>` underneath it, groups by that value, and
emits a JSON map. Cases in `--plan-cases` but missing from the XML come
back as `not-implemented`.

---

## Contracts and conventions

### The `qa_case` pytest marker

Registered in `pyproject.toml`. Usage:

```python
@pytest.mark.qa_case("CASE-042")
def test_thing_under_test():
    ...
```

One marker per test. Only the first is captured by the conftest hook.

### Dispatch payload schema

| Field | Required? | Notes |
|---|---|---|
| `service` | yes | Short slug — directory name of the service repo. |
| `pr_number` | yes | Issue/PR number, as a string. |
| `head_sha` | yes | Full SHA. Used for the commit status target and the cross-repo checkout. |
| `head_ref` | no | Branch name. Currently unused; reserved for v2.5+. |
| `originating_repo` | yes | Full `owner/name`. |
| `qa_plan_comment_id` | no | Reserved for v2.5+ — the roadmap-issue comment ID containing the `<!-- qa-test-plan:v1 -->` body. |
| `plan_cases` | no | Comma-separated CASE-IDs. Defaults to all 18 known cases. |
| `linked_prs` | no | JSON map of `{repo_name: branch_or_sha}` for cross-service PR deps. Empty `{}` default; not consumed yet. |

### Sentinel comments

| Sentinel | Posted by | Lives on |
|---|---|---|
| `<!-- engineering-triage-breakdown:v1 -->` | openclaw engineering agent | roadmap issue |
| `<!-- qa-test-plan:v1 -->` | openclaw QA agent | roadmap issue |
| `<!-- integration-test-results:v1 -->` | this runner | the PR in the service repo |
| `<!-- qa-execution-report:v1 -->` | openclaw QA agent (planned) | roadmap issue |

### The `jarvis-integration` commit status

`context: jarvis-integration`. Renders on the PR's HEAD SHA. Green if
all cases pass; red if any `fail` or `not-implemented`.

---

## Secrets and permissions

### `INTEGRATION_DISPATCH_TOKEN`

| | |
|---|---|
| Lives in | each participating service repo's secrets (today: `jarvis-command-center`) |
| Used by | `integration-trigger.yml` |
| Resource owner | `alexberardi` |
| Repository access | `Only select repositories` → `jarvis-node-setup` |
| Permissions | **Contents: Read and write** |

### `INTEGRATION_COMMENT_TOKEN`

| | |
|---|---|
| Lives in | `jarvis-node-setup` secrets |
| Used by | `integration-runner.yml` (comment + status post steps) |
| Resource owner | `alexberardi` |
| Repository access | `Only select repositories` → every participating service repo |
| Permissions | **Pull requests: Read and write** + **Commit statuses: Read and write** |

### GHCR package access

The workflow uses `GITHUB_TOKEN` with `permissions: packages: read` to
pull `ghcr.io/alexberardi/jarvis-auth:dev` and `:config-service:dev`.
For private packages, each one's "Manage Actions access" must include
the runner repo. One-time setup:

- https://github.com/users/alexberardi/packages/container/jarvis-auth/settings → Manage Actions access → Add Repository → `jarvis-node-setup`
- https://github.com/users/alexberardi/packages/container/jarvis-config-service/settings → same

(Public packages skip this step.)

### Storing secrets

```bash
gh secret set INTEGRATION_DISPATCH_TOKEN --repo alexberardi/<service>
gh secret set INTEGRATION_COMMENT_TOKEN --repo alexberardi/jarvis-node-setup
```

Verify:

```bash
gh secret list --repo alexberardi/<service>
gh secret list --repo alexberardi/jarvis-node-setup
```

---

## Onboarding a new participating service repo

1. **Copy the trigger workflow.** Drop a copy of `integration-trigger.yml`
   into the target repo's `.github/workflows/`. Change
   `client_payload[service]` to the target repo's directory name.
2. **Add `INTEGRATION_DISPATCH_TOKEN` to the new repo.**
3. **Extend `INTEGRATION_COMMENT_TOKEN`'s scope** to include the new
   repo and re-store the secret.
4. **Update the runner's `bring_up_cc` logic** if the new service needs
   its own compose path. v2.13 plans a more generic
   `bring_up_service_under_test` so this is just a payload-driven
   selector.
5. **Open a trivial PR** in the new repo to validate.

---

## Adding a new QA case

1. Number the case in the QA plan (`CASE-NNN`).
2. Write a test decorated `@pytest.mark.qa_case("CASE-NNN")` in
   `tests/` (not `tests/integration/`).
3. Either update `plan_cases` in the workflow's default, or have the
   trigger send it explicitly in the `client_payload`.

A case in `plan_cases` but not in the XML → `not-implemented` (red
status). A marker in the XML not in `plan_cases` → still appears in the
table; the parser doesn't filter.

---

## Adding a canned LLM / Whisper response

Edit `tests/fakes/canned_responses.yaml`. First regex match wins.

```yaml
responses:
  - prompt_regex: "(?i)what.?s? the weather"
    response:
      role: assistant
      content: ""
      stop_reason: tool_calls
      tool_calls:
        - id: call_w1
          function:
            name: get_weather
            arguments: '{"location": "current"}'

transcripts:
  - filename_regex: "weather.*\\.wav$"
    transcript: "What's the weather today"
```

---

## Operator runbook

### Manually re-fire the runner against an existing PR

```bash
gh workflow run integration-runner.yml \
  --repo alexberardi/jarvis-node-setup \
  --ref main \
  -f service=jarvis-command-center \
  -f pr_number=4 \
  -f head_sha=<full SHA from PR's tip> \
  -f originating_repo=alexberardi/jarvis-command-center \
  -f plan_cases="CASE-001,CASE-002,CASE-003,CASE-101,CASE-102,CASE-103,CASE-104,CASE-201,CASE-202,CASE-203,CASE-204,CASE-205,CASE-206,CASE-207,CASE-208,CASE-209,CASE-210,CASE-211"
```

### Force a re-run by pushing an empty commit

```bash
git commit --allow-empty -m "ci: re-fire integration loop" && git push
```

### Reproduce a failure locally (against running stack)

```bash
# 1. Bring up the deps phase
docker compose -f docker-compose.ci.yaml --profile core up -d --wait \
  postgres jarvis-auth jarvis-config-service mosquitto

# 2. Run the seed
AUTH_URL=http://localhost:7701 \
CONFIG_URL=http://localhost:7700 \
AUTH_ADMIN_TOKEN=ci-auth-admin-token \
CONFIG_ADMIN_TOKEN=ci-auth-admin-token \
GITHUB_ENV=/tmp/seed.env \
  bash compose/seed.sh

# 3. Export the seeded values
source /tmp/seed.env  # exports CC_APP_KEY, JARVIS_CC_APP_KEY, CFG_APP_KEY, CC_HOUSEHOLD_ID

# 4. Bring up CC with the seeded key
CC_SOURCE_PATH=../jarvis-command-center \
JARVIS_CC_APP_KEY=$JARVIS_CC_APP_KEY \
  docker compose -f docker-compose.ci.yaml --profile core up -d --wait \
    jarvis-command-center

# 5. Phase 2.5 — register node in CC
NODE_RESP=$(curl -sS -X POST http://localhost:7703/api/v0/admin/nodes \
  -H "X-API-Key: ci-admin-key" \
  -H "Content-Type: application/json" \
  -d "{\"node_id\":\"ci-node-001\",\"household_id\":\"$CC_HOUSEHOLD_ID\",\"room\":\"ci-room\",\"name\":\"CI Node\"}")
echo "CC /admin/nodes: $NODE_RESP"
export CC_NODE_ID="ci-node-001"
export CC_NODE_KEY=$(echo "$NODE_RESP" | python3 -c "import json,sys; print(json.load(sys.stdin)['node_key'])")

# 6. Start the fakes
python -m tests.fakes.fake_llm_backend --port 7705 &
python -m tests.fakes.fake_whisper      --port 7706 &

# 7. Run pytest
FAKE_LLM_URL=http://127.0.0.1:7705 \
FAKE_WHISPER_URL=http://127.0.0.1:7706 \
CC_URL=http://localhost:7703 \
AUTH_URL=http://localhost:7701 \
CONFIG_URL=http://localhost:7700 \
CC_APP_ID=command-center \
CC_APP_KEY=$CC_APP_KEY \
CC_NODE_ID=$CC_NODE_ID \
CC_NODE_KEY=$CC_NODE_KEY \
  pytest tests/test_loop_smoke.py tests/test_cc_real_smoke.py \
    --junit-xml=/tmp/results.xml -v

# 8. Inspect parsed results
python tools/parse_junit.py /tmp/results.xml \
  --plan-cases "CASE-001,CASE-002,CASE-003,CASE-101,CASE-102,CASE-103,CASE-104,CASE-201,CASE-202,CASE-203,CASE-204,CASE-205,CASE-206,CASE-207,CASE-208,CASE-209,CASE-210,CASE-211"

# 9. Cleanup
docker compose -f docker-compose.ci.yaml --profile core down -v
kill %1 %2 2>/dev/null
```

### Inspect a failed run

```bash
RUN_ID=$(gh run list --repo alexberardi/jarvis-node-setup \
                     --workflow integration-runner.yml \
                     --limit 1 --json databaseId --jq '.[0].databaseId')

gh run view $RUN_ID --repo alexberardi/jarvis-node-setup
gh run view $RUN_ID --repo alexberardi/jarvis-node-setup --log-failed
gh run view $RUN_ID --repo alexberardi/jarvis-node-setup --log
```

### Inspect the result on a PR

```bash
gh pr view <pr> --repo alexberardi/<service> --json comments \
  --jq '[.comments[] | select(.body | contains("integration-test-results:v1"))] | last | .body'

gh pr view <pr> --repo alexberardi/<service> --json statusCheckRollup \
  --jq '.statusCheckRollup | map(.name + " -> " + (.conclusion // .status))'
```

---

## Current limitations (v2.12)

1. **Only `jarvis-command-center` is wired.** Other service repos can
   open PRs but won't trigger this loop. **v2.13** fans out the trigger.
2. **No real LLM proxy, Whisper, or TTS.** Fakes only. Real GPU services
   are v3 territory (self-hosted Ubuntu CUDA runner + macOS-15 MLX, both
   path-gated).
3. **No assertion on audio *content*.** `CASE-206`/`CASE-209` prove
   bytes flow end-to-end and headers come from the fake TTS, but the
   audio is 32 bytes of zero PCM — we don't verify any audible
   synthesis happened. A real test would need a real TTS, which moves
   us into v3 territory.
4. **No speaker-resolution test.** Whisper can return a `speaker`
   field with `{user_id, confidence}` for household voice profiles;
   CC's command-center uses that for memory injection. Our fake
   returns `speaker: {user_id: None, confidence: 0.0}`. A real
   speaker-resolution case would require either real voice profiles
   in whisper or stubbing the speaker resolver in CC.
5. **MQTT → node async channel not exercised.** CC publishes settings
   updates, TTS-by-text, package installs, bluetooth ops, reminders,
   etc. via MQTT topics (see jarvis-node-setup/CLAUDE.md "MQTT
   background thread"). The test loop has mosquitto in the compose
   stack but no test posts to or subscribes from it. A future case
   would publish a known message and assert CC's MQTT-driven handler
   processed it.
6. **Mixed server+client tool branch not covered.** `CASE-211` exercises
   the all-client multi-tool branch. CC also has a mixed branch where
   a single LLM response contains both a server tool (e.g.
   `request_validation`) and a client tool — CC runs the server tool
   first, then continues the loop so the LLM can see the server result
   before the client tool fires. A future case would canned-respond
   with a server+client mix and assert the two-iteration flow.
4. **Plan cases are hardcoded** in the workflow's default. The QA agent
   will eventually pass `plan_cases` in the trigger payload once we
   update the trigger.
5. **One test = one case.** Multiple `qa_case` markers on the same test
   capture only the first.
6. **Commit status, not check-run.** Fine-grained PATs can't post
   check-runs (Checks API is GitHub-App-only).
7. **Concurrency is per-PR, cancel-in-progress.** A burst of pushes
   cancels earlier runs.
8. **No manual-required workflow.** Hardware-needing test cases (real
   Pi mic, mobile UI) have no clean way to surface as
   `action_required`. v2.13+ candidate.
9. **GHA `repository_dispatch` only fires workflows on the default
   branch.** Changes to `integration-runner.yml` only take effect *after*
   merging to `main`. Test runner changes via
   `workflow_dispatch --ref <branch>` while iterating.
10. **The openclaw QA agent doesn't yet read the result comment.** The
    `<!-- qa-execution-report:v1 -->` sentinel and the Pi-side prompt
    update are deferred (the user mentioned they're being worked on
    separately).
11. **No incremental test selection.** Every run executes every test.
12. **Failure excerpt is truncated** to 240 chars in the comment. Full
    stack traces only in the CI run logs.
13. **`linked_prs` plumbed through but not consumed.** When v2.5+ lands
    a multi-service-PR test scenario, build steps will read it.
14. **`compose down -v` between runs** wipes all the seed data, so
    every PR run re-seeds. That's the right behavior for isolation but
    could be optimized in v3 with a snapshotted Postgres volume.

---

## Common failure modes we've actually hit

Real symptoms from real runs across v1 → v2.3, with the fix.

### `actions/checkout` rejects `path: ../jarvis-command-center`

`Repository path '/home/runner/work/jarvis-node-setup/jarvis-command-center' is not under '/home/runner/work/jarvis-node-setup/jarvis-node-setup'`.

**Fix**: check out into `_src/jarvis-command-center` (inside workspace),
set `CC_SOURCE_PATH=./_src/jarvis-command-center` when invoking compose.
(Fixed in PR #8.)

### CC's alembic crashes on `CREATE EXTENSION IF NOT EXISTS vector`

The stock `postgres:15` image doesn't ship pgvector. CC stores memory
embeddings via pgvector.

**Fix**: use `pgvector/pgvector:pg15`. (Fixed in PR #10.)

### `Fakes never became healthy` + `Form data requires "python-multipart" to be installed`

`fake_whisper.py` uses `UploadFile`, which makes FastAPI require
`python-multipart` at import time.

**Fix**: add `python-multipart` to the runner's pip install. (Fixed in
v2.0 era.)

### Tests run but every case reports `not-implemented`

Either pytest never produced XML (collection failed) or the qa_case
hook didn't fire.

**Check**: is the test under `tests/integration/`? That subtree's
conftest imports the production codebase which needs `jarvis_command_sdk`.
Move the test up to `tests/`. (Fixed in v1 era.)

### Seed step exits with `JSONDecodeError: Expecting value: line 1 column 2 (char 1)`

`curl -sf` was silently returning empty body on non-2xx, then
`python3 -c "json.load(sys.stdin)"` died with a useless traceback.

**Fix**: `http_post()` helper that captures status + body and dumps both
on failure. (Fixed in v2.3 era, PR #13.)

### Seed step gets right JSON from auth but still fails to parse

`log()` was writing to stdout, so `$(register_app_client ...)` captured
`"[seed] Registering app-client: command-center\n{json}"` — python
choked on the first line.

**Fix**: `log() { echo "[seed] $*" >&2; }`. (Fixed in PR #14.)

### CASE-201 returns `404 Not Found for /internal/validate-app`

Auth doesn't expose `/internal/validate-app` — app credentials are
checked inline on every protected endpoint. The exploration agent
flagged this; I missed it the first time.

**Fix**: change `CASE-201` to call `/internal/validate-node` with a
bogus node; assert `valid: false` (which means app-auth succeeded but
node lookup failed). (Fixed in PR #15.)

### Trigger workflow fires but no runner appears

The receiver isn't on `jarvis-node-setup`'s default branch. Either
`integration-runner.yml` hasn't merged yet, or you triggered from a
branch. `repository_dispatch` only fires workflows from the default
branch.

### Trigger workflow fails with 401/403 on `gh api .../dispatches`

`INTEGRATION_DISPATCH_TOKEN` missing, expired, or scoped wrong.
Confirm it has `Contents: Read and write` on `jarvis-node-setup`.

### Runner pull fails with `denied: pull access denied` from ghcr.io

The runner repo isn't in the package's "Manage Actions access" list,
or the package is private without that allowlist. See "GHCR package
access" above.

### "Process completed with exit code 4" annotation but run shows success

The pytest step has `continue-on-error: true` on purpose — we want
the result comment posted even when tests fail. The annotation appears
when *any* step exits non-zero. Look at the comment, not the run
status.

---

## Roadmap

### v2.13 — fan-out (next)

- Copy `integration-trigger.yml` to remaining service repos. Each gets
  its own `INTEGRATION_DISPATCH_TOKEN` secret; extend
  `INTEGRATION_COMMENT_TOKEN` scope accordingly.
- Generalize `bring_up_cc` to `bring_up_service_under_test` — branch on
  `service` in the payload, build the corresponding service from its
  PR source.
- Add the `manual-required` check-run flow for hardware-dependent
  cases.

### v3 — real GPU testing

- Register the Ubuntu desktop (`10.0.0.122`) as a GHA self-hosted
  runner with `[self-hosted, linux, cuda]` labels.
- Add `gpu-llm-cuda` job in the runner with `--gpus all`, path-gated
  to `jarvis-llm-proxy-api/**`.
- Add `gpu-llm-mlx` on `macos-15-xlarge` (10× minute multiplier — keep
  the path filter strict).

---

## Where the openclaw side picks this up

The Pi at `pi@10.0.0.245` runs the openclaw agentic workflow. Three
agent prompts there will need updates to consume this layer's outputs:

| Path | Update |
|---|---|
| `/home/pi/.openclaw/qa-prompt.md` | QA plan must use stable `CASE-NNN` IDs, mark `manual: true|false` per case, and (new phase) read the `<!-- integration-test-results:v1 -->` comment after coding-agent's PR opens → post a `<!-- qa-execution-report:v1 -->` sentinel on the roadmap issue. |
| `/home/pi/.openclaw/coding-prompt.md` | New tests written by coding-agent must use `@pytest.mark.qa_case("CASE-N")`. Tests without a marker fail QA traceability. |
| `/home/pi/.openclaw/workspaces/qa/CONTEXT.md` | Document both sentinels with example bodies. |

These updates are tracked separately by the user.

---

## File index

Paths relative to `jarvis-node-setup` unless noted.

| Path | Purpose |
|---|---|
| `.github/workflows/integration-runner.yml` | Receives dispatches, runs the two-phase compose + tests + posts results |
| `docker-compose.ci.yaml` | CI stack definition: pgvector + mosquitto + auth + config-service + CC |
| `compose/postgres-init.sh` | Creates jarvis_auth + jarvis_config DBs on first init |
| `compose/seed.sh` | Phase-1.5 seed: registers app-clients in auth, captures keys, registers fakes in config-service, registers a CI user via `/auth/register` and exports `CC_HOUSEHOLD_ID`. Node registration itself is in the Phase 2.5 workflow step (because it needs CC up). |
| `tests/fakes/__init__.py` | (empty — package marker) |
| `tests/fakes/fake_llm_backend.py` | FastAPI shim for `jarvis-llm-proxy-api` (non-streaming + SSE) |
| `tests/fakes/fake_whisper.py` | FastAPI shim for `jarvis-whisper-api` |
| `tests/fakes/fake_tts.py` | FastAPI shim for `jarvis-tts` (added v2.8 for the audio path) |
| `tests/fakes/canned_responses.yaml` | Canned data for the LLM + Whisper fakes |
| `tests/conftest.py` | `qa_case` marker → JUnit user-property hook |
| `tests/test_loop_smoke.py` | v1 fakes-only suite (CASE-001…003) |
| `tests/test_cc_real_smoke.py` | v2.1+ real-stack suite (CASE-101…104, 201…206) |
| `tools/__init__.py` | (empty — package marker) |
| `tools/parse_junit.py` | JUnit XML → case-status JSON |
| `pyproject.toml` | Registers the `qa_case` pytest marker |
| `docs/integration-tests.md` | This document |
| `CLAUDE.md` (this repo) | Brief pointer to this doc |
| `CLAUDE.md` (jarvis-command-center) | Brief pointer to this doc |

In `jarvis-command-center`:

| Path | Purpose |
|---|---|
| `.github/workflows/integration-trigger.yml` | Fires `repository_dispatch` on PR events with the v2.3 payload (incl. linked_prs) |

On the openclaw Pi (`pi@10.0.0.245`):

| Path | Purpose |
|---|---|
| `/home/pi/.openclaw/qa-prompt.md` | QA agent prompt (Pi-side updates tracked separately) |
| `/home/pi/.openclaw/coding-prompt.md` | Coding-agent prompt |
| `/home/pi/.openclaw/triage-prompt.md` | Engineering agent prompt |
| `/home/pi/.openclaw/workspaces/qa/CONTEXT.md` | QA agent operating contract |
| `/home/pi/integration-tests.md` | Copy of this doc (kept in sync via scp) |
| `/home/pi/we-are-in-the-lucky-brook.md` | The original plan file |
