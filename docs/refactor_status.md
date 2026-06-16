# voice_listener.py refactor — status

**Status: COMPLETE. All 11 phases shipped to dev.** Pure-refactor TDD
pass that broke the 2,538-line monolith into focused modules under
`core/`. The end-state file responsibilities are cleanly bounded.

**State as of last touch (2026-06-04 ~15:03 EDT):**
- Local working tree: clean, all 324 refactor-related tests green.
- <dev-node>.local: phases 1-11 all live. Service restarted
  2026-06-04 15:02:36 EDT; "Waiting for wake word" reached at
  15:03:17. No exceptions in journal.
- Final sizes:
  - `scripts/voice_listener.py` — **320 lines** (was 2,538, -87%)
  - `core/wake_loop.py` — **661 lines** (new, owns the wake loop)
  - Plus 9 other `core/*` modules from earlier phases.
- The unrelated SDK Alert fix still needs to be committed + pushed +
  deployed to prod — see `project_sdk_alert_fix_undeployed.md`.
- kitchen prod: untouched. Won't move until everything tags + ships.

---

## Pattern used (the rhythm that worked)

Every phase had the same shape:

1. **RED** — write `tests/test_<module>.py` against the future module
   path `core.<module>`. Source the corpus from real prod incidents
   when possible; characterize *existing* behavior, don't redesign.
   Confirm the test file fails with `ModuleNotFoundError`.

2. **GREEN** — create `core/<module>.py` by moving the functions +
   constants out of `voice_listener.py` verbatim. Drop the leading
   underscore from any function that's now a public API of the module.
   Keep internal helpers underscored. Run tests; they pass.

3. **REFACTOR** — in `voice_listener.py`, add the import, delete the
   moved code block, replace_all the renamed call sites. Run an AST
   sanity parse + the shadow scan (see "Soak gotchas" below).

4. **DEPLOY** — `scp` the new module + updated `voice_listener.py` to
   `pi@<dev-node>.local:/opt/jarvis-node/`, then run a Python import
   probe on the Pi to confirm everything loads. **Restart between
   phases.** Each restart catches its own latent shadow bugs.

Diagnostics to ignore: Pyright `reportMissingImports` errors on local
imports — they're a project path-config issue, not real bugs. Tests
run green from the project root.

---

## All 11 phases

| # | Module | New module file | Tests | Lines removed | Notes |
|---|---|---|---|---|---|
| 1 | `false_wake` | `core/false_wake.py` | 37 | ~110 | abort phrases + multi-sentence/segment shape detection |
| 2 | `conversation_filters` | `core/conversation_filters.py` | 41 | ~100 | self-echo guard, follow-up noise, assistant-text probe |
| 3 | `vad_thresholds` | `core/vad_thresholds.py` | 30 | ~65 | barge-in config + adaptive silence math. **Soak gotcha** — see below |
| 4 | `wake_calibration` | `core/wake_calibration.py` | 23 | ~95 | p20 calibration, persisted score history |
| 5 | `music_control` | `core/music_control.py` | 23 | ~330 | full music ducking (move/mute/SIGSTOP), is_playing, energy multiplier |
| 6 | `wake_response` | `core/wake_response.py` | 37 | ~275 | chime + LED + cached TTS ack + warmup + processing-ack pre-cache |
| 7 | `wake_detector` | `core/wake_detector.py` | 20 | ~148 | three-gate wake-fire pipeline returned as a `WakeVerdict` |
| 8 | `wake_transcription` | `core/wake_transcription.py` | 24 | ~211 | STT round-trip + CC orchestration |
| 9 | `follow_up_loop` | `core/follow_up_loop.py` | 20 | ~292 | the brittleness epicenter — three-layer guard architecture |
| 10 | `alert_announcer` | `core/alert_announcer.py` | 17 | ~67 | high-priority alert TTS during quiet wake-loop moments + `has_pending_high_priority_alerts` probe added in phase 11 |
| 11 | `wake_loop` | `core/wake_loop.py` | 12 | ~554 | The main wake loop itself. `start_voice_listener` reduces to init/cleanup; `run_wake_loop(...)` owns the while-true cycle. |

**Cumulative:** `scripts/voice_listener.py` is now **320 lines**
(2,538 → 320, -87%). New code lives in `core/` modules with their
own focused tests. **324 new tests across the refactor.**

---

## Phase 11 specifics — what was decided and what was changed

**Decomposition choice: option 2 (extract `core/wake_loop.py`).**
The seam between "set up resources" (openWakeWord, AudioBus, AEC,
command/STT/validation services) and "run the wake cycle" is natural.
`run_wake_loop(...)` takes 7 params (within the 6-8 bound the original
plan called for); module-level cross-cutting state (`_bg_executor`,
`_wake_paused`) is injected via `set_runtime(...)` — the same pattern
already in `wake_response` and `follow_up_loop`.

**Latent bug surfaced and fixed:** the outer `finally` block of the
wake-fire path read `result.get("on_response_complete")` before
`result` was guaranteed to be assigned. Any exception propagating
through `handle_keyword_detected` / `warmup_thread.start()` /
`BargeInMonitor()` construction would hit the finally with `result`
unbound and raise `UnboundLocalError`, masking the original. Moved the
`result: dict | None = None` initialization to the top of the wake-fire
try block. Behavior change is bug-fix-only: clean shutdown instead of
crash on early exceptions.

**Two cleanups landed alongside phase 11:**

1. **Dead branch removed** in `wake_transcription.send_for_transcription`.
   The `else: speak_error("I couldn't understand that, sorry.")` arm
   at the bottom was unreachable because `is_non_speech("")` returns
   True at the top and short-circuits empty/whitespace text. Dedented
   the if-body; left a comment noting the invariant. The
   characterization test (`test_empty_text_drops_silently_via_non_speech_path`)
   still passes — it asserts the dead path is NOT taken.

2. **`has_pending_high_priority_alerts()` extracted** from the inline
   wake-loop probe into `core.alert_announcer`. Wake loop no longer
   imports `services.alert_queue_service` or
   `ALERT_ANNOUNCE_PRIORITY` directly — the queue concern lives in
   one module. Five new tests cover the helper (empty / only-low /
   high / mixed / queue-exception).

---

## Soak gotchas (lessons from the 2026-06-04 restarts)

Two pre-existing latent bugs surfaced during the phase 1-9 restart at
12:46 EDT. Both follow the same pattern: a phase-3 rename dropped the
leading underscore on an extracted function name, which then collided
with a `name = name(...)` assignment in `start_voice_listener`.
Annotated LHS made the name local; RHS lookup became unbound;
`UnboundLocalError` at runtime.

A third latent bug surfaced during the phase 11 test run (above) — the
`result` UnboundLocalError.

**Mitigation now in place:**

1. AST shadow scan at the end of every phase (see
   `feedback_restart_before_refactor_extend.md`).
2. **Restart dev between phases** rather than chaining unrestarted
   work. Each restart catches its own batch of latent issues; chained
   phases hide them under successive layers of "still on old code."
3. Phase 11 also added KeyboardInterrupt-injecting tests that exercise
   the outer finally and surfaced the `result` bug deterministically.

---

## What's still open

1. **Commit the cumulative refactor work.** The user's call: one big
   coherent commit, or one commit per phase preserving the TDD
   history? Phase numbers + module names are already meaningful labels
   either way. The branch is `feat/wake-during-music` (already named
   from an earlier audio investigation; the refactor went on top).
2. **Tag and ship to kitchen prod** once committed.
3. **The SDK Alert fix is still uncommitted.** Dev has it; prod
   doesn't. See `project_sdk_alert_fix_undeployed.md` — needs commit,
   push, prod install, prod restart.

---

## Critical caveats — please read before doing anything destructive

1. **The `not_for_me` cool-down gate is gone, not paused.** A wake
   immediately after a `not_for_me` verdict is accepted. The 8-second
   debounce (same-utterance double-fire) remains.

2. **The SDK `Alert` shape changed.** It now has `created_at`,
   `expires_at`, `id`, `is_expired`, `to_dict` — all with defaults so
   existing callers don't break. Pantry packages that produce Alerts
   continue to work without modification.

3. **`music_control` dropped the `_duck_music`/`_restore_music` aliases.**
   They were only used inside `voice_listener.py` itself — no external
   consumer. If you find one later, just import
   `pause_active_playback` / `resume_active_playback` from
   `core.music_control` directly.

4. **No tracemalloc on the Pi Zero 2W**, ever — the user reports it
   has locked up the device hard historically. Use
   `/proc/<pid>/smaps_rollup` snapshots over time for allocation
   evidence instead.

5. **Don't touch kitchen** until the whole refactor is tagged and the
   user explicitly pushes. The deploy model is tag-and-push, not
   continuous-deploy.

---

## Quick-start to resume

```bash
cd /Users/alexanderberardi/jarvis/jarvis-node-setup

# Confirm state
git status
git diff --stat

# Run the full refactor test suite (324 tests, <1 s)
python -m pytest tests/test_false_wake.py tests/test_conversation_filters.py \
                 tests/test_vad_thresholds.py tests/test_wake_calibration.py \
                 tests/test_music_control.py tests/test_wake_response.py \
                 tests/test_wake_detector.py tests/test_wake_transcription.py \
                 tests/test_follow_up_loop.py tests/test_alert_announcer.py \
                 tests/test_wake_loop.py tests/test_voice_listener_filters.py \
                 tests/test_alert_queue_service.py tests/test_whats_up_command.py -q

# AST shadow scan
python -c "
import ast
for path in ['scripts/voice_listener.py', 'core/wake_loop.py']:
    src = open(path).read()
    tree = ast.parse(src)
    imports = {n.asname or n.name.split('.')[-1]
               for node in tree.body
               if isinstance(node, (ast.Import, ast.ImportFrom))
               for n in node.names}
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Assign, ast.AnnAssign)):
                    targets = sub.targets if isinstance(sub, ast.Assign) else [sub.target]
                    for t in targets:
                        if isinstance(t, ast.Name) and t.id in imports:
                            hits.append(f'L{sub.lineno}: {t.id} in {node.name}')
    print(path, 'SHADOWS:' if hits else 'no shadows')
    for h in hits: print(' ', h)
"
```
