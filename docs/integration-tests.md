# Integration tests (GHA-based)

This doc covers the v1 GitHub-Actions runner that closes the QA test-execution
gap: when a PR opens in a Jarvis service repo, a cross-service integration
suite runs here and posts results back on the PR.

## TL;DR

- `.github/workflows/integration-runner.yml` — receives `repository_dispatch`
  events of type `pr-integration` and runs the integration suite.
- `tests/fakes/` — FastAPI shims for `jarvis-llm-proxy-api` and
  `jarvis-whisper-api` so the default lane runs on free Linux runners
  without GPU.
- `tests/integration/test_loop_smoke.py` — the v1 smoke test exercising
  the fakes. Each test has a `@pytest.mark.qa_case("CASE-NNN")` marker.
- `tools/parse_junit.py` — joins pytest's JUnit XML output to QA-plan case
  IDs and emits JSON.

## How a PR triggers a run

1. PR opens in a participating service repo (v1: `jarvis-command-center`).
2. That repo's `.github/workflows/integration-trigger.yml` posts a
   `repository_dispatch` to `alexberardi/jarvis-node-setup` with
   `event_type=pr-integration` and a `client_payload` carrying
   `service`, `pr_number`, `head_sha`, `head_ref`, `originating_repo`, and
   (optionally) `qa_plan_comment_id`.
3. `integration-runner.yml` picks up the dispatch and:
   - Checks out this repo.
   - Starts the fake LLM and Whisper backends as background processes
     (`tests/fakes/fake_llm_backend.py`, `tests/fakes/fake_whisper.py`).
   - Runs `pytest tests/integration/test_loop_smoke.py --junit-xml=results.xml`.
   - Runs `tools/parse_junit.py` to map QA-plan cases to pass/fail/skipped/
     not-implemented and produce `results.json`.
   - Renders a Markdown comment with the case-by-case breakdown.
   - Posts the comment on the originating PR with the
     `<!-- integration-test-results:v1 -->` sentinel.
   - Posts a `jarvis-integration` commit status on the PR's HEAD SHA
     (success if all cases pass, failure otherwise). Renders next to the
     commit on the PR — same UX as a check-run, but uses the Statuses API
     so a PAT can post it (Checks API is GitHub-App-only).

## Required secrets

| Secret | Lives in | Permission | Why |
|---|---|---|---|
| `INTEGRATION_DISPATCH_TOKEN` | each participating service repo | `repository_dispatch:write` on `alexberardi/jarvis-node-setup` | Lets the trigger workflow fire dispatch events. |
| `INTEGRATION_COMMENT_TOKEN` | `jarvis-node-setup` | `pull-requests:write` and `commit-statuses:write` on each participating service repo | Lets the runner post the result comment and commit status back to the originating PR. |

Use fine-grained PATs scoped to only the listed repos and permissions.

## Adding the integration trigger to a new service

Drop a copy of `integration-trigger.yml` into the service's
`.github/workflows/` directory, change the `client_payload[service]` value to
match the service's directory name, and add the `INTEGRATION_DISPATCH_TOKEN`
secret to that repo. That's it — nothing else changes on the runner side
because the receiver keys off the payload, not a hard-coded list.

## Adding a new QA case

1. **In the QA plan** (roadmap-issue comment with the
   `<!-- qa-test-plan:v1 -->` sentinel), number the case as `CASE-NNN`.
2. **In code**, decorate a pytest test:
   ```python
   @pytest.mark.qa_case("CASE-042")
   def test_thing_under_test():
       ...
   ```
3. The conftest hook in `tests/conftest.py` copies the marker into
   `item.user_properties`, which pytest serializes into JUnit XML as
   `<property name="qa_case" value="CASE-042"/>`.
4. `parse_junit.py` keys on that property name.

Tests without a `qa_case` marker still run, but they don't appear in the
QA execution report — they're treated as ambient coverage, not plan
coverage.

## Adding a canned LLM or Whisper response

Edit `tests/fakes/canned_responses.yaml`:

```yaml
responses:                        # LLM prompts
  - prompt_regex: "(?i)set a timer"
    response:
      role: assistant
      content: ""
      stop_reason: tool_calls
      tool_calls:
        - id: call_001
          function:
            name: set_timer
            arguments: '{"duration_seconds": 300}'

transcripts:                      # Whisper inputs (matched on filename)
  - filename_regex: "timer.*\\.wav$"
    transcript: "Set a five minute timer"
```

First match wins. Unmatched prompts fall back to a generic OK response;
unmatched filenames fall back to "fake transcript". Keep regexes loose
enough that small wording changes in the test don't break the match.

## Reproducing a CI failure locally

```bash
# In a venv with deps installed
pip install pytest pytest-asyncio fastapi uvicorn httpx pydantic pyyaml

# Start the fakes
python -m tests.fakes.fake_llm_backend --port 7705 &
python -m tests.fakes.fake_whisper --port 7706 &

# Run the suite
FAKE_LLM_URL=http://127.0.0.1:7705 \
FAKE_WHISPER_URL=http://127.0.0.1:7706 \
pytest tests/integration/test_loop_smoke.py --junit-xml=results.xml -v

# Inspect the parsed results
python tools/parse_junit.py results.xml \
    --plan-cases "CASE-001,CASE-002,CASE-003"
```

## Re-running the loop manually

The runner workflow accepts `workflow_dispatch` inputs so you can re-run it
against an existing PR while iterating on the runner itself:

```
gh workflow run integration-runner.yml \
    --repo alexberardi/jarvis-node-setup \
    -f service=jarvis-command-center \
    -f pr_number=42 \
    -f head_sha=<sha> \
    -f originating_repo=alexberardi/jarvis-command-center
```

## Roadmap (v2, v3)

This v1 deliberately stops short of bringing up a real Jarvis stack — the
loop runs only the fakes-based smoke suite. Future iterations:

- **v2.** Add a `core` docker-compose profile that brings up the actual
  service-under-test (e.g. CC) with the fakes still substituting for
  LLM+Whisper. Fan the trigger workflow out to the remaining service
  repos. Add the `manual-required` check-run flow for cases that need
  hardware.
- **v3.** Register the Ubuntu desktop as a self-hosted runner with the
  `cuda` label; add a `gpu-llm-cuda` job for real-backend LLM tests.
  Add `gpu-llm-mlx` on `macos-15-xlarge` gated by a path-filter on
  `jarvis-llm-proxy-api/**`.

The full design lives in
`/Users/alexanderberardi/.claude/plans/we-are-in-the-lucky-brook.md`
(also on the openclaw Pi at `/home/pi/we-are-in-the-lucky-brook.md`).
