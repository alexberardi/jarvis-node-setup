# QA Integration Test Execution Layer

A GitHub-Actions-based CI loop that closes the post-PR test-execution gap in
the openclaw agentic workflow. When a coding-agent (or human) opens a PR in
a participating service repo, this layer runs a marker-bound pytest suite
against the PR's HEAD SHA and posts a structured result comment plus a
commit status back on the PR — without granting the QA agent any runtime
or write permissions.

The QA agent on the Pi stays read-only by design. CI does the work; QA
will eventually read the result comment (next phase) and post a
`<!-- qa-execution-report:v1 -->` sentinel back on the originating roadmap
issue.

---

## Mental model

Two GitHub repos, one runner, two scoped PATs:

```
┌────────────────────────────────────────────┐         ┌────────────────────────────────────────────┐
│ Service repo (e.g. jarvis-command-center)  │         │ jarvis-node-setup                          │
│                                             │         │                                             │
│  .github/workflows/                         │         │  .github/workflows/                         │
│    integration-trigger.yml                  │         │    integration-runner.yml                   │
│                                             │         │                                             │
│  on: pull_request                           │         │  on: repository_dispatch                    │
│      ↓                                       │  POST    │      types: [pr-integration]               │
│  gh api .../dispatches  ───────────────────▶│ ──────▶ │      ↓                                       │
│      (uses INTEGRATION_DISPATCH_TOKEN)      │         │  - boot fake_llm + fake_whisper             │
│                                             │         │  - pytest tests/test_loop_smoke.py          │
│                                             │  POST    │  - parse_junit → JSON                       │
│  PR comment ◀──────────────────────────────│ ◀────── │  - render comment + post                    │
│  + jarvis-integration commit status         │         │      (uses INTEGRATION_COMMENT_TOKEN)        │
└────────────────────────────────────────────┘         └────────────────────────────────────────────┘
```

The runner is a single point of change. Each service repo only needs the
~40-line trigger workflow — it knows nothing about *how* tests run.

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
       "qa_plan_comment_id": ""
     }
   }
   ```

   Auth: `INTEGRATION_DISPATCH_TOKEN` secret in the originating repo.
3. **`integration-runner.yml`** in `jarvis-node-setup` is listening for
   that dispatch type *on its default branch* (`repository_dispatch` only
   fires workflows from the default branch — that's why the runner has to
   be on `main` before anything routes to it).
4. **Resolve the payload** into step outputs (`service`, `pr_number`,
   `head_sha`, `originating_repo`, `plan_cases`). Falls back to
   `workflow_dispatch` inputs when re-triggered manually.
5. **Set up Python 3.11** + cache pip + install deps:
   `pytest pytest-asyncio fastapi uvicorn httpx pydantic pyyaml python-multipart`.
6. **Start the fakes as background processes:**

   ```bash
   nohup python -m tests.fakes.fake_llm_backend --port 7705 &
   nohup python -m tests.fakes.fake_whisper      --port 7706 &
   ```

   Both load `tests/fakes/canned_responses.yaml` for their canned data.
7. **Wait for `/health`** on both fakes (20s timeout). On timeout, dump
   logs and exit non-zero.
8. **Run pytest:**

   ```bash
   FAKE_LLM_URL=http://127.0.0.1:7705 \
   FAKE_WHISPER_URL=http://127.0.0.1:7706 \
   pytest tests/test_loop_smoke.py --junit-xml=results.xml -v
   ```

   Each test has `@pytest.mark.qa_case("CASE-NNN")`. The hook in
   `tests/conftest.py` copies that marker into `item.user_properties`,
   which pytest then serializes into the XML as
   `<property name="qa_case" value="CASE-001"/>`. This step has
   `continue-on-error: true` so the workflow keeps going even if tests
   fail — we want results posted either way.
9. **Parse the XML:**

   ```bash
   python tools/parse_junit.py results.xml \
     --plan-cases "CASE-001,CASE-002,CASE-003" \
     --run-url "$RUN_URL" \
     --output results.json
   ```

   Output shape:

   ```json
   {
     "run_url": "https://github.com/.../actions/runs/26009844544",
     "cases": {
       "CASE-001": {
         "status": "pass",
         "test_name": "tests.test_loop_smoke::test_fake_llm_returns_canned_completion",
         "failure_excerpt": ""
       },
       "CASE-007": {
         "status": "not-implemented",
         "test_name": "",
         "failure_excerpt": "No test found with this qa_case marker."
       }
     },
     "summary": {
       "total": 4,
       "pass": 3,
       "fail": 0,
       "skipped": 0,
       "not_implemented": 1
     }
   }
   ```

   Cases in `--plan-cases` but missing from the XML come back as
   `not-implemented` so missing coverage is visible.
10. **Render `comment.md`** with the `<!-- integration-test-results:v1 -->`
    sentinel as the first line. Inline Python heredoc; output is a
    Markdown table plus a `### Failures` block with failure excerpts when
    relevant.
11. **Post to the originating PR** via
    `gh pr comment $PR_NUMBER --repo $ORIGINATING_REPO --body-file comment.md`.
    Auth: `INTEGRATION_COMMENT_TOKEN` secret in `jarvis-node-setup`.
12. **Post a commit status** on the PR's HEAD SHA via
    `POST /repos/{owner}/{repo}/statuses/{sha}` with:

    ```
    state:       success | failure
    context:     jarvis-integration
    description: "3/3 pass" or "0/3 pass · 0 fail · 3 not-implemented"
    target_url:  <CI run URL>
    ```

    Same token. (Checks API would be richer — annotations, multi-step UI
    — but it requires a GitHub App, which fine-grained PATs can't be.)
13. **Cleanup**: kill the fake PIDs (`if: always()`), dump their stdout
    logs so debugging a failed run only needs the run page.

Round-trip from `git push` to comment landing on the PR is ~30 seconds
with a warm pip cache.

---

## Component reference

### `.github/workflows/integration-trigger.yml` (each participating service repo)

Tiny — just fans the `pull_request` event out to a `repository_dispatch`.
Lives in every repo we want covered. Today: `jarvis-command-center` only.

| | |
|---|---|
| Trigger | `pull_request: [opened, synchronize, reopened]` targeting `main` |
| Auth | `INTEGRATION_DISPATCH_TOKEN` secret in this repo |
| Target | `/repos/alexberardi/jarvis-node-setup/dispatches` |
| Payload | `{service, pr_number, head_sha, head_ref, originating_repo, qa_plan_comment_id}` |
| Timeout | 2 min (it's just an API call) |

The workflow is the *only* place to set `service` per repo — that field
is how the receiver knows which service the PR belongs to.

### `.github/workflows/integration-runner.yml` (jarvis-node-setup)

The receiver and orchestrator. Lives in `jarvis-node-setup` because that
repo already plays the role of "central place for cross-service tests"
(see `test_multi_turn_conversation.py` etc.).

| | |
|---|---|
| Trigger | `repository_dispatch: types: [pr-integration]` + `workflow_dispatch` |
| Manual inputs | `service`, `pr_number`, `head_sha`, `originating_repo`, `qa_plan_comment_id`, `plan_cases` |
| Concurrency group | `integration-<originating_repo>-<pr_number>` with `cancel-in-progress: true` |
| Timeout | 15 min (hard cap; in practice ~30s) |
| Required secret | `INTEGRATION_COMMENT_TOKEN` (this repo) |

Notable design points:
- `workflow_dispatch` lets you re-fire the runner manually against any
  PR without touching the trigger flow — handy while iterating on the
  runner itself.
- The concurrency group key is per-PR, so a new push to the same PR
  cancels the in-flight run rather than queueing.
- `continue-on-error: true` on the pytest step + `if: always()` on the
  parse/render/post steps means a *broken* run still produces a useful
  comment + red status.
- If pytest never produces `results.xml` (e.g., collection failed),
  the parse step synthesizes `<?xml version="1.0"?><testsuites/>` so the
  parser reports every planned case as `not-implemented` instead of
  crashing.

### `tests/fakes/fake_llm_backend.py`

FastAPI shim mimicking `jarvis-llm-proxy-api`. Endpoint: `POST /v1/chat`.
Reads `canned_responses.yaml`, regex-matches the latest user-role message
body (first match wins), returns the configured `response` object.
Unmatched prompts return `{role: assistant, content: "OK", stop_reason: complete}`.

```bash
python -m tests.fakes.fake_llm_backend --port 7705 \
    --responses tests/fakes/canned_responses.yaml
```

Env overrides: `FAKE_LLM_PORT`, `FAKE_LLM_RESPONSES`.

### `tests/fakes/fake_whisper.py`

FastAPI shim mimicking `jarvis-whisper-api`. Endpoint: `POST /transcribe`
(multipart). Regex-matches the uploaded audio filename against
`canned_responses.yaml` `transcripts` entries; unmatched filenames return
`"fake transcript"`.

```bash
python -m tests.fakes.fake_whisper --port 7706
```

Env overrides: `FAKE_WHISPER_PORT`, `FAKE_WHISPER_RESPONSES`.

**Important**: this shim uses `UploadFile`, so FastAPI requires
`python-multipart` at import time. The runner workflow installs it
explicitly — leaving it out causes the fake to fail to start and every
case to report `not-implemented`.

### `tests/fakes/canned_responses.yaml`

Single file feeding both fakes. Two top-level keys:

```yaml
responses:                                    # consumed by fake_llm_backend
  - prompt_regex: "what'?s? \\d+ plus \\d+"   # case-insensitive
    response:
      role: assistant
      content: "The result is 62."
      stop_reason: complete

  - prompt_regex: "(set|start) (a |the )?(\\d+ ?\\w+ )?timer"
    response:
      role: assistant
      content: ""
      stop_reason: tool_calls
      tool_calls:
        - id: call_001
          function:
            name: set_timer
            arguments: '{"duration_seconds": 300, "label": "test"}'

transcripts:                                  # consumed by fake_whisper
  - filename_regex: "timer.*\\.wav$"
    transcript: "Set a five minute timer"
```

The `response` object is returned verbatim under `message`; the LLM
shim's response shape is `{id, model, message: <response>}`. The
`tool_calls` field is optional and follows OpenAI-style structure.

### `tests/conftest.py` — the `qa_case` marker hook

```python
def pytest_collection_modifyitems(items):
    for item in items:
        for marker in item.iter_markers(name="qa_case"):
            case_id = marker.args[0] if marker.args else None
            if case_id:
                item.user_properties.append(("qa_case", case_id))
```

Runs once during pytest collection. For every test decorated with
`@pytest.mark.qa_case("CASE-NNN")`, copies the case ID into
`item.user_properties`. Pytest's JUnit serializer turns those into
`<property name="qa_case" value="CASE-NNN"/>` elements, which is what
`parse_junit.py` keys on.

The hook deliberately omits the `config` parameter — pytest hook
discovery inspects the function signature, so leaving `config` out
keeps the linter from complaining about an unused parameter.

### `tests/test_loop_smoke.py`

Three smoke cases (`CASE-001`, `CASE-002`, `CASE-003`) that exercise
both fakes via `httpx`. Asserts the fakes return their canned content,
which simultaneously verifies:

- The fakes booted and responded to network requests in CI.
- Pytest's exit status flows back through the workflow correctly.
- The `qa_case` marker → JUnit `<property>` pipeline is intact (because
  `parse_junit.py` reports `pass` for the three CASE-IDs, not
  `not-implemented`).

**Lives at `tests/test_loop_smoke.py`, not `tests/integration/test_loop_smoke.py`.**
The integration subtree has its own `conftest.py` that imports the
production codebase (which depends on `jarvis_command_sdk`). Pytest
loads conftests from the project root down to the test file, so even
running just this smoke test from inside `tests/integration/` triggered
that import chain and failed collection with `ModuleNotFoundError`. The
smoke suite is intentionally SDK-free — its job is to prove the loop
works against the fakes, nothing more.

### `tools/parse_junit.py`

Stdlib-only (`xml.etree.ElementTree`). Walks every `<testcase>`, looks
for a `qa_case` `<property>` underneath it, groups by that value, and
emits a JSON map (see the output shape in "End-to-end flow" step 9).

```bash
python tools/parse_junit.py results.xml \
  --plan-cases "CASE-001,CASE-002" \
  --run-url "https://..." \
  --output results.json
```

- `--plan-cases` is comma-separated. Any case in this list that doesn't
  appear in the XML is added with `status: not-implemented`.
- `--run-url` becomes a top-level field in the JSON output for the
  rendered comment's "CI run" link.
- Without `--output`, JSON goes to stdout.

`failure_excerpt` is truncated to 240 chars (with an ellipsis if
clipped) to keep the rendered PR comment short. Full logs live in the
linked CI run.

---

## Contracts and conventions

### The `qa_case` pytest marker

Registered in `pyproject.toml`:

```toml
[tool.pytest.ini_options]
markers = [
    "qa_case(id): map this test to a QA plan case (e.g. CASE-001). The id is exported to JUnit XML for the integration-runner to join against the QA plan.",
]
```

Usage:

```python
import pytest

@pytest.mark.qa_case("CASE-042")
def test_timer_cancels_after_set(timer_service):
    ...
```

One marker per test. The argument is a free-form string but the QA plan
convention is `CASE-NNN`. The marker's value is what surfaces in the
PR comment's `Case` column.

Multiple markers on the same test are allowed but only the *first* is
captured by the conftest hook (`marker.args[0]`). If you have a test
that genuinely covers two QA cases, parameterize or duplicate; one
test == one case is the cleanest model.

### Dispatch payload schema

The trigger sends, the runner consumes. The receiver tolerates missing
optional fields.

| Field | Required | Notes |
|---|---|---|
| `service` | yes | Short slug — directory name of the service repo. Used for routing logic when we add per-service test profiles. |
| `pr_number` | yes | Issue/PR number in the originating repo, as a string. |
| `head_sha` | yes | Full SHA. Used both for the commit status target and (later) for checking out the PR's tip. |
| `head_ref` | no  | Branch name. Currently unused; reserved for v2 when we check out the service repo at PR head. |
| `originating_repo` | yes | Full `owner/name`. Both repo segments are needed for cross-repo posting. |
| `qa_plan_comment_id` | no  | Reserved for v2 — the roadmap-issue comment ID containing the `<!-- qa-test-plan:v1 -->` body. QA agent will use it to map cases back to the plan. |
| `plan_cases` | no  | Comma-separated CASE-IDs. If omitted, the runner falls back to `CASE-001,CASE-002,CASE-003` (the v1 smoke set). |

### JUnit XML serialization

The `qa_case` value lands in the XML like this:

```xml
<testsuite name="pytest" tests="3">
  <testcase classname="tests.test_loop_smoke"
            name="test_fake_llm_returns_canned_completion"
            time="0.012">
    <properties>
      <property name="qa_case" value="CASE-001"/>
    </properties>
  </testcase>
  ...
</testsuite>
```

`parse_junit.py` keys on the `name="qa_case"` attribute. Other marker
hooks that write to `user_properties` won't collide as long as they use
a different `name`.

### The `<!-- integration-test-results:v1 -->` sentinel

A stable HTML comment as the *first line* of the rendered PR comment.
Future tooling (next-phase QA agent, dashboards, etc.) keys off this
exact string to find the comment without fuzzy matching:

```bash
gh pr view <pr> --repo <repo> --json comments \
  --jq '[.comments[] | select(.body | contains("integration-test-results:v1"))] | last | .body'
```

Versioning: the `v1` suffix is intentional. When the comment format
changes incompatibly, bump to `v2` and update consumers.

Adjacent sentinels in the agentic workflow (defined on the Pi side):

| Sentinel | Posted by | Lives on |
|---|---|---|
| `<!-- engineering-triage-breakdown:v1 -->` | engineering agent | roadmap issue |
| `<!-- qa-test-plan:v1 -->` | QA agent | roadmap issue |
| `<!-- integration-test-results:v1 -->` | this runner | the PR in the service repo |
| `<!-- qa-execution-report:v1 -->` | QA agent (planned v2) | roadmap issue |

### The `jarvis-integration` commit status

`context: jarvis-integration` is the stable name. Renders on the PR's
HEAD SHA same way a check would — green if all cases pass, red
otherwise. The `target_url` points at the CI run for one-click access
to logs.

---

## Secrets and permissions

Both are fine-grained PATs (web-UI-only to create; `gh` can store them
as secrets via `gh secret set --repo ...` afterwards).

### `INTEGRATION_DISPATCH_TOKEN`

| | |
|---|---|
| Lives in | each participating service repo's secrets (today: `jarvis-command-center`) |
| Used by | `integration-trigger.yml` |
| Resource owner | `alexberardi` |
| Repository access | `Only select repositories` → `jarvis-node-setup` |
| Permissions | **Contents: Read and write** (required by `POST /dispatches`) |

### `INTEGRATION_COMMENT_TOKEN`

| | |
|---|---|
| Lives in | `jarvis-node-setup` secrets |
| Used by | `integration-runner.yml` (post-comment and post-status steps) |
| Resource owner | `alexberardi` |
| Repository access | `Only select repositories` → every participating service repo |
| Permissions | **Pull requests: Read and write** + **Commit statuses: Read and write** |

When you add a new participating service repo, extend the
`INTEGRATION_COMMENT_TOKEN`'s repository scope to include it — the token
needs to be able to post comments + statuses into the new repo.

### Storing the secrets

```bash
gh secret set INTEGRATION_DISPATCH_TOKEN --repo alexberardi/<service>
# paste token, enter

gh secret set INTEGRATION_COMMENT_TOKEN --repo alexberardi/jarvis-node-setup
# paste token, enter
```

Verify:

```bash
gh secret list --repo alexberardi/<service>
gh secret list --repo alexberardi/jarvis-node-setup
```

---

## Onboarding a new participating service repo

Five steps. Total time: ~5 minutes.

1. **Copy the trigger workflow.** Drop a copy of
   `jarvis-command-center/.github/workflows/integration-trigger.yml`
   into the target repo's `.github/workflows/`.
2. **Update the `service` payload field.** Change
   `client_payload[service]=jarvis-command-center` to match the new
   repo's directory name (e.g. `client_payload[service]=jarvis-tts`).
3. **Add the `INTEGRATION_DISPATCH_TOKEN` secret** to the new repo:
   `gh secret set INTEGRATION_DISPATCH_TOKEN --repo alexberardi/<new>`.
4. **Extend `INTEGRATION_COMMENT_TOKEN`'s scope** to include the new
   repo (regenerate the fine-grained PAT or edit its repository list,
   then update the secret on `jarvis-node-setup`).
5. **Open a trivial PR** in the new repo to validate the trigger fires
   and the runner posts back.

No changes to the runner workflow are required — the routing is
data-driven.

---

## Adding a new QA case

1. **Update the QA plan comment** (the `<!-- qa-test-plan:v1 -->` body
   on the roadmap issue): assign the new case a stable ID
   (`CASE-NNN`), short name, arrange/act/assert intent, and
   `manual: true|false`.
2. **Write the test** somewhere pytest can find it (today: under
   `tests/`, eventually wherever the cross-service suite lives):

   ```python
   import pytest

   @pytest.mark.qa_case("CASE-042")
   def test_thing_under_test():
       ...
   ```

3. **Update `--plan-cases`** in the workflow — or, better, pass it via
   the dispatch payload from the trigger so each service's plan-cases
   list can vary independently. v1 hardcodes
   `CASE-001,CASE-002,CASE-003` as a fallback; passing
   `plan_cases` in the trigger's `client_payload` overrides this.

A case in `--plan-cases` but not in the XML reports as
`not-implemented` (red status). A test with a `qa_case` marker not in
`--plan-cases` still appears in the comment table — the parser doesn't
filter by the plan list, it adds to it.

---

## Adding a canned LLM / Whisper response

Edit `tests/fakes/canned_responses.yaml`. Both fakes hot-reload only on
process restart (which happens every CI run), so editing the YAML and
re-running is enough.

```yaml
responses:
  # New entry for a "what's the weather" intent
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

**Tips for regexes:**
- Use `(?i)` for case-insensitive matching unless the test specifically
  cares about casing.
- Keep them loose — the test's value comes from what comes *out* of the
  full pipeline, not from the fake's prompt parsing.
- First match wins. Put more specific patterns *before* generic ones in
  the file.

---

## Operator runbook

### Manually re-fire the runner against an existing PR

Helpful when iterating on `integration-runner.yml` itself, or when you
want to verify a token rotation without waiting for a new push.

```bash
gh workflow run integration-runner.yml \
  --repo alexberardi/jarvis-node-setup \
  --ref main \
  -f service=jarvis-command-center \
  -f pr_number=4 \
  -f head_sha=<full SHA from the PR's tip> \
  -f originating_repo=alexberardi/jarvis-command-center \
  -f plan_cases="CASE-001,CASE-002,CASE-003"
```

Then watch:

```bash
gh run watch \
  $(gh run list --repo alexberardi/jarvis-node-setup --workflow integration-runner.yml --limit 1 --json databaseId --jq '.[0].databaseId') \
  --repo alexberardi/jarvis-node-setup
```

### Force a re-run by pushing an empty commit

Cheapest way to re-fire the *full* trigger → runner chain (matches what
a fresh `synchronize` event would do):

```bash
git commit --allow-empty -m "ci: re-fire integration loop" && git push
```

### Reproduce a failure locally

```bash
# in a venv
pip install pytest pytest-asyncio fastapi uvicorn httpx pydantic pyyaml python-multipart

# start the fakes
python -m tests.fakes.fake_llm_backend --port 7705 &
python -m tests.fakes.fake_whisper      --port 7706 &

# run the suite
FAKE_LLM_URL=http://127.0.0.1:7705 \
FAKE_WHISPER_URL=http://127.0.0.1:7706 \
pytest tests/test_loop_smoke.py --junit-xml=/tmp/results.xml -v

# parse, same as CI
python tools/parse_junit.py /tmp/results.xml \
  --plan-cases "CASE-001,CASE-002,CASE-003" \
  --run-url "local"

# cleanup
kill %1 %2 2>/dev/null
```

### Inspect a failed run

```bash
RUN_ID=$(gh run list --repo alexberardi/jarvis-node-setup \
                     --workflow integration-runner.yml \
                     --limit 1 --json databaseId --jq '.[0].databaseId')

# overall step status
gh run view $RUN_ID --repo alexberardi/jarvis-node-setup

# log for failed steps only
gh run view $RUN_ID --repo alexberardi/jarvis-node-setup --log-failed

# all logs
gh run view $RUN_ID --repo alexberardi/jarvis-node-setup --log
```

### Inspect the result on a PR

```bash
# the comment
gh pr view <pr> --repo alexberardi/<service> --json comments \
  --jq '[.comments[] | select(.body | contains("integration-test-results:v1"))] | last | .body'

# the commit status
gh pr view <pr> --repo alexberardi/<service> --json statusCheckRollup \
  --jq '.statusCheckRollup | map(.name + " -> " + (.conclusion // .status))'
```

---

## Current limitations (v1)

Be honest with yourself when writing tests against this layer.

1. **Only `jarvis-command-center` is wired to the trigger.** v2 fans the
   trigger workflow out to the remaining ~15 service repos. Other repos
   can open PRs and they won't be tested by this layer yet.
2. **No real Jarvis services run.** The runner never brings up CC, LLM
   proxy, Whisper, TTS, Postgres, Redis, MQTT, etc. Tests can only
   exercise behavior reachable through the two fakes' canned responses.
   "Did the LLM actually choose the right tool" is *not* answerable by
   the v1 smoke loop — it's a question for v2 with the real stack.
3. **Fakes are stateless.** Each request is matched independently — no
   conversation memory, no rate limiting, no token usage tracking. If a
   test needs cross-call state, you'll have to add a fixture that
   constructs it locally.
4. **Fakes only know what's in `canned_responses.yaml`.** Anything
   outside the regex set falls back to a generic stub
   (`{role: assistant, content: "OK", stop_reason: complete}` for LLM,
   `"fake transcript"` for Whisper). A test that depends on a
   specific response must add a canned entry.
5. **The smoke test is the only suite that runs.** `pytest tests/test_loop_smoke.py`
   is hardcoded in `integration-runner.yml`. Cross-service tests
   (`test_multi_turn_conversation.py` etc.) are *not* invoked. v2 will
   change this once a compose stack lands.
6. **Plan cases are hardcoded per-run.** The `--plan-cases` list comes
   from the workflow input or the dispatch payload. There's no
   automatic discovery from the roadmap issue yet. The QA agent will
   need to pass `plan_cases` in the trigger's `client_payload` once
   we update the trigger workflow.
7. **Each test maps to exactly one case.** If a test has multiple
   `qa_case` markers, only the first is captured. Use parameterization
   or duplicate test functions when one logical behavior covers two
   plan cases.
8. **Commit status, not check-run.** No multi-step UI, no annotations,
   no inline file comments. Fine-grained PATs can't post check-runs
   (Checks API is GitHub-App-only). All detail is in the PR comment.
9. **Concurrency is per-PR, cancel-in-progress.** A burst of pushes to
   the same PR will cancel earlier runs — that's intentional, but it
   means you can't see partial results from an interrupted run.
10. **No manual-required workflow.** v2 plans an `action_required`
    check-run for hardware-dependent cases that need human verification.
    Today, "manual" cases just appear as `not-implemented` because
    there's no marker to express "this should be tested by a human".
11. **GHA `repository_dispatch` only fires workflows on the default
    branch.** Changes to `integration-runner.yml` are only effective
    *after* merging to `main`. Test runner changes via
    `workflow_dispatch --ref <branch>` while iterating, or accept the
    PR → merge → trial-fire cycle.
12. **The QA agent on the Pi doesn't yet read the result comment.**
    The `<!-- qa-execution-report:v1 -->` sentinel and the prompt
    update are the next phase. Until then, the comment + status are
    informational only — no agent enforces them.
13. **No incremental test selection.** Every run executes every test
    in the smoke suite. v2 with path-filtering will only run tests
    relevant to the changed paths.
14. **Failure excerpt is truncated.** `parse_junit.py` clips to 240
    chars per failure. Full stack traces live in the CI run logs only.
15. **`python-multipart` must be in the runner's pip install.** It's
    a transitive requirement of `fake_whisper.py` (via `UploadFile`).
    Removing it silently breaks the loop. (We hit this once already.)
16. **The QA case ID is a string.** No validation that it matches
    `CASE-NNN` or that the same ID isn't reused across tests in
    different files. The parser will silently overwrite earlier
    entries with later ones if duplicates exist.
17. **The runner doesn't yet check out the service repo.** It only
    checks out `jarvis-node-setup` itself. v2 will check the
    originating service at `head_sha` so the running tests can
    actually exercise the code being changed; v1's smoke suite
    doesn't need this because it only touches the fakes.

---

## Common failure modes we've actually seen

Real symptoms from real runs, so you can recognize them fast.

### Symptom: `0 pass | 0 fail | 0 skipped | N not-implemented`

Tests didn't run. Either the runner's "Wait for fakes" step failed (so
pytest was skipped) or pytest itself couldn't collect (so no XML was
produced).

Check in order:
- `gh run view <id> --log-failed` — look for the failing step.
- Fakes' stdout is dumped at the end of the workflow under
  `=== fake_llm.log ===` and `=== fake_whisper.log ===`. If a fake
  exited with a Python traceback, that's your bug.
- If pytest itself failed at collection (`ImportError`, `ModuleNotFoundError`),
  check whether the test file is under `tests/integration/` — that
  subtree's conftest needs `jarvis_command_sdk`.

### Symptom: `Fakes never became healthy` followed by `Form data requires "python-multipart" to be installed`

`fake_whisper.py` failed to import because the runner's pip install
omitted `python-multipart`. Add it back to the `pip install ...` line
in `integration-runner.yml`. (Fixed in commit `6855b21`.)

### Symptom: Runner ran but the comment didn't post

The runner's check-run/comment-post step doesn't have permission to
write to the originating repo. Check:
- `INTEGRATION_COMMENT_TOKEN` exists in `jarvis-node-setup`'s secrets.
- The token's repository scope includes the originating repo.
- The token has both **Pull requests: Read and write** *and*
  **Commit statuses: Read and write** (it needs both — one for the
  comment, one for the status).

### Symptom: Trigger workflow fires but no runner appears

The receiver isn't on `jarvis-node-setup`'s default branch. Either:
- `integration-runner.yml` hasn't been merged to `main` yet.
- You triggered from a branch but `repository_dispatch` only fires
  workflows on the default branch.

Use `workflow_dispatch --ref <branch>` for branch-local runs.

### Symptom: Trigger workflow fails with 401 / 403

`INTEGRATION_DISPATCH_TOKEN` is missing, expired, or scoped wrong.
Verify:
- Secret is set: `gh secret list --repo alexberardi/<service>`.
- Token has **Contents: Read and write** on `jarvis-node-setup`.
- Token hasn't expired (fine-grained PATs expire by default).

### Symptom: PR comment shows test_name as `tests.integration.test_loop_smoke::...`

The smoke test was moved out of `tests/integration/` to `tests/`. If
you see the old path, you're looking at a comment from before the move
PR merged (`95961d0`). Push a fresh empty commit to re-fire.

### Symptom: "Process completed with exit code 4" annotation but run shows success

GHA shows the annotation when *any* step exits non-zero, but the run
itself is marked `success` because the failing step has
`continue-on-error: true` (the pytest step does this on purpose). Look
at the comment, not the run status.

---

## Roadmap

### v2 — fan-out and full-stack tests

- Copy `integration-trigger.yml` to all remaining service repos. Each
  gets its own `INTEGRATION_DISPATCH_TOKEN` secret; extend the
  `INTEGRATION_COMMENT_TOKEN` scope accordingly.
- Add a `core` docker-compose profile that brings up the actual
  service-under-test plus its direct dependencies (still with the
  fakes substituting for LLM + Whisper to keep the default lane
  GPU-free).
- Have the runner check out the originating service repo at
  `head_sha` (alongside this repo) so tests can exercise the actual
  changed code.
- Path-filter routing so only tests relevant to the changed paths run.
- Add the `manual-required` check-run flow for cases that need
  hardware verification (real Pi mic, mobile UI, etc.).
- Move the four top-level `test_*.py` scripts from the repo root
  (`test_command_parsing.py`, `test_multi_turn_conversation.py`,
  `test_full_pipeline.py`, `test_tool_calling.py`) into
  `tests/integration/` and invoke them from the runner.
- QA agent prompt update on the Pi to read the
  `<!-- integration-test-results:v1 -->` comment and post a
  `<!-- qa-execution-report:v1 -->` sentinel on the roadmap issue.

### v3 — real GPU testing

- Register the Ubuntu desktop (`10.0.0.122`) as a GHA self-hosted
  runner with `[self-hosted, linux, cuda]` labels. Runs as
  unprivileged `gha-runner` user; jobs containerized with `--gpus all`.
- Add `gpu-llm-cuda` job in `integration-runner.yml`,
  `runs-on: [self-hosted, cuda]`, path-filtered to
  `jarvis-llm-proxy-api/**` or `jarvis-whisper-api/**` changes.
- Add `gpu-llm-mlx` on `macos-15-xlarge`, same path filter. Mind the
  10× minute multiplier — keep this filter tight.
- Both jobs report standard PR check-runs.

---

## Writing a test against this layer (for agents and humans)

Decision tree for a new QA case:

1. **Can the case be expressed against the existing fakes?**
   - Yes → add canned responses to `canned_responses.yaml`, write the
     test against the fakes via `httpx`, add the `qa_case` marker.
     This is the v1 happy path — fast, reliable, runs on free
     compute.
   - No, it needs real services → flag as "blocked on v2" in the QA
     plan. The case can still be specified now; it just won't run
     automatically until v2 lands.
   - No, it needs hardware → flag as `manual: true` in the QA plan.
     v2 will surface these as `action_required` checks; today they
     just sit as not-implemented.

2. **Where should the test file live?**
   - Smoke tests / fakes-only: `tests/<short_name>.py` (not under
     `tests/integration/`).
   - Tests that need real Jarvis production code imports: `tests/integration/`
     after v2 lands. Don't add them there yet — the runner doesn't
     install the SDK.

3. **Does the test need a new canned response?**
   - Yes → add to `tests/fakes/canned_responses.yaml`. Test the regex
     locally with `python -c "import re; print(re.search(r'...', '...', re.I))"`
     before committing.
   - No → use existing entries or accept the fallback.

4. **Did you add the marker?**
   - `@pytest.mark.qa_case("CASE-NNN")` — exact format. Without it,
     the test still runs but doesn't show up in the comment.

5. **Did you pass `plan_cases` in the dispatch?**
   - If the new CASE-ID isn't in `CASE-001,CASE-002,CASE-003`, either
     update the trigger to pass a longer `plan_cases` list, or accept
     that the case will just show up if-and-only-if it has a marker
     match. The `--plan-cases` list only matters for "not-implemented"
     surfacing.

---

## File index

Paths are all relative to `jarvis-node-setup` unless noted.

| Path | Purpose |
|---|---|
| `.github/workflows/integration-runner.yml` | Receives dispatches, runs tests, posts back |
| `tests/fakes/__init__.py` | (empty — package marker) |
| `tests/fakes/fake_llm_backend.py` | FastAPI shim for `jarvis-llm-proxy-api` |
| `tests/fakes/fake_whisper.py` | FastAPI shim for `jarvis-whisper-api` |
| `tests/fakes/canned_responses.yaml` | Canned data for both fakes |
| `tests/conftest.py` | `qa_case` marker → JUnit user-property hook |
| `tests/test_loop_smoke.py` | v1 smoke suite (3 cases) |
| `tools/__init__.py` | (empty — package marker) |
| `tools/parse_junit.py` | JUnit XML → case-status JSON |
| `pyproject.toml` | Registers the `qa_case` pytest marker |
| `docs/integration-tests.md` | This document |
| `CLAUDE.md` (this repo) | Brief pointer to this doc |
| `CLAUDE.md` (jarvis-command-center) | Brief pointer to this doc |

In `jarvis-command-center`:

| Path | Purpose |
|---|---|
| `.github/workflows/integration-trigger.yml` | Fires `repository_dispatch` on PR events |

On the openclaw Pi (`pi@10.0.0.245`, **not yet updated for v1**):

| Path | Purpose |
|---|---|
| `/home/pi/.openclaw/qa-prompt.md` | QA agent prompt (needs `CASE-NNN` rule + result-reading phase) |
| `/home/pi/.openclaw/coding-prompt.md` | Coding-agent prompt (needs `@pytest.mark.qa_case` rule) |
| `/home/pi/.openclaw/triage-prompt.md` | Engineering agent prompt |
| `/home/pi/.openclaw/workspaces/qa/CONTEXT.md` | QA agent operating contract |
