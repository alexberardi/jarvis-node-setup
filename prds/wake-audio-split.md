# PRD: split wake/audio loop into a dedicated process

**Status:** **DORMANT** as of 2026-06-08. Phases 1, 2, 2b, 2c, and 3 are structurally complete and sit on `feat/wake-audio-split` as a single WIP commit. The split is NOT shipped to main and NOT running on any node.

**Why dormant:** Soak on jarvis-dev measured the per-process memory floor at ~349 MB (main 120 MB + audio 229 MB) on a 416 MB Pi Zero 2W. That overshoots the implicit two-process budget by ~70 MB — exactly the gating threshold in Q3 of this PRD ("if we blow past 300 MB the split hasn't helped"). Symptom on the box: any apt-install while both processes are alive thrashes swap until apt times out at 300 s, and a fresh-boot launch of both processes OOM-kills sshd / the HAT LED service before the system settles. The split delivered on its primary goal (restart-able audio process, isolated tool state) but on Pi Zero 2W the per-process overhead negates the win.

**What survives (already on main):**
- The leak the soak found: `command_discovery_service` background-poll re-imports, fixed in a separate small commit. Benefits both modes.
- `services/heap_census.py` — SIGUSR2 diagnostic for future leak hunts.
- `prds/audacy-mpd-install-gap.md` — separate bug surfaced during 2c soak validation, fix shipped end-to-end.

**Owner branch:** `feat/wake-audio-split` off main. WIP commit preserves the full structural work for revisit when running on Pi 3A+/4/Zero 3W or otherwise increasing RAM. Do not delete the branch without checking first.

**Decisions locked (carry-forward):** Option A (audio owns hot path), source under `jarvis-node-setup/audio_process/`, AEC moves with audio (still WIP, not abandoned), liveness/execute timeout split, file-based metrics, manual DoD on `jarvis-dev.local`, `tts_playback.py` carve-out NOT needed (`core/platform_audio.py` already abstracts aplay/pulse), command-discovery still in audio after Phase 2 — Phase 2c (TBD) moves it.

## Problem

The Pi Zero 2W runs everything in one Python process: openWakeWord + AudioBus + follow-up VAD + TTS playback + tool execution + Pantry agents + MQTT control plane + settings sync. RSS grows over time (calendar agent, log client, package loaders, third-party deps). When growth crosses the budget the swap thrashes, page faults stall the wake loop, and the AudioBus starts dropping the wake subscriber's chunks — which the user experiences as a multi-second delay between "hey jarvis" and the chime/ack.

Evidence (prod, last 6 h, 2026-06-05):

| node | room | drops | last restart |
|---|---|---|---|
| 5a384f63 | bedroom | 3,546 in 5 h | not recent — worst case |
| 9d744b30 | kitchen | 635, then 0 | 00:54 restart cleared |
| d6eeef94 | living_room | 819, then 0 | 23:48 restart cleared |

The restart-clears pattern is the signature of a leak, not steady-state load. We've already done the in-process cleanups we know about (`project_pi_memory_fixes`, `project_pi_agent_memory_2026_05`, the 11-phase `voice_listener.py` refactor). The next move is structural: isolate the real-time audio loop from the long-running heap.

## Goals

1. Wake-loop process has a bounded, predictable RSS profile — when it does grow, it can be restarted independently in <2 s without disrupting tool/agent state.
2. AudioBus subscriber drop rate goes to ~0 in steady state on jarvis-dev over a 48 h soak.
3. Wake-fire → first-audio-ack latency stays under existing baseline (~1 s).
4. The agent/package side can leak memory or get killed without losing the user's voice command.
5. A clean enough boundary that the audio process could later run on a separate microcontroller / coprocessor without rearchitecting.

## Non-goals

- Replacing openWakeWord, the STT path, or the TTS path.
- Changing the CC ↔ node HTTP contract. CC sees the same client.
- Cross-node audio routing or multi-room playback. Out of scope here.
- Fixing the underlying leak. We're isolating, not curing.

## Architecture (Option A — audio owns the hot path)

```
                  ┌────────────────────────────────────────────────────┐
                  │  jarvis-audio (new process, ~150-200 MB RSS)       │
                  │                                                     │
   mic ──────────▶│  AudioBus → wake_detector → wake_response          │
                  │  follow_up_loop → wake_transcription ──HTTP───▶ CC │
                  │  TTS audio ◀──HTTP── CC                            │
                  │  aplay/pulse playback                              │
                  │  barge-in monitor, music_control, alert_announcer  │
                  │                                                     │
                  │  IPC server on /run/jarvis/audio.sock              │
                  └──────────────────┬─────────────────────────────────┘
                                     │  Unix socket
                                     │  length-prefixed JSON frames
                                     │  (+ binary body for audio)
                  ┌──────────────────▼─────────────────────────────────┐
                  │  jarvis-node (existing, slimmer)                   │
                  │                                                     │
                  │  IJarvisCommand registry + tool execution          │
                  │  Pantry, agents (calendar/news/weather/zwave/HA)   │
                  │  MQTT listener (settings/packages/BT/updates)      │
                  │  Heartbeat, settings sync, provisioning            │
                  │  Encrypted secrets storage, K2 mgmt                │
                  │                                                     │
                  │  IPC client to audio process                       │
                  └────────────────────────────────────────────────────┘
```

### Hot path

```
[audio]  wake fires
[audio]  POST /conversation/start to CC
[audio]  POST /voice/command/stream to CC with captured audio
[audio]  CC returns 200 audio  →  play through pulse
         OR
[audio]  CC returns 202 JSON with tool_calls
[audio]  for each tool_call: IPC → main: execute_tool {id, name, args}
[main]   tool_executor.execute(...)
[main]   IPC → audio: tool_result {id, output, context}
[audio]  POST /voice/command/continue/stream with results
[audio]  CC returns audio  →  play
```

### Control plane (main → audio)

- `play_alert {audio_bytes, priority, source}` — when an agent in main produces a high-priority alert (calendar reminder, etc.). **Main is responsible for calling `jarvis-tts` to synthesize the bytes**; audio just plays them. Frame size isn't a concern for the sizes we use; if it becomes one we can switch to a "fetch your own TTS" model later. Audio plays during a quiet wake-loop moment.
- `pause_audio_loop` / `resume_audio_loop` — **interrupts current playback immediately.** Used for setup/upgrade flows AND for ducking when the user wants to speak a command over music/radio. On `resume`, the wake loop comes back up; previously-playing audio does NOT auto-resume — that's the caller's responsibility if it matters.
- `set_setting {key, value}` — when MQTT-pushed settings change a value the audio process cares about (wake threshold, VAD thresholds, voice_mode).

## IPC protocol

Unix domain socket at `/run/jarvis/audio.sock`, owned by `pi:pi`, mode 0660.

**Frame format:** 4-byte big-endian header length, JSON header, optional 4-byte big-endian body length, binary body. Header is always JSON; body is raw bytes (audio).

```
[hdr_len: u32 BE][hdr JSON utf-8][body_len: u32 BE | 0][body bytes]
```

**Required header fields:** `type` (str), `id` (uuid, for request/response correlation), `body_len` (int, mirrors the framing byte).

**Message types:**

| Direction | type | Body? | Purpose |
|---|---|---|---|
| audio → main | `execute_tool` | no | request tool execution; expects `tool_result` |
| main → audio | `tool_result` | no | response with output + context |
| main → audio | `play_alert` | yes (audio) | priority alert to speak when quiet |
| main → audio | `pause_audio_loop` | no | stop wake detection for setup |
| main → audio | `resume_audio_loop` | no | resume wake detection |
| main → audio | `set_setting` | no | settings change notification |
| audio → main | `request_setting` | no | one-shot setting fetch on startup |
| main → audio | `setting_value` | no | response to `request_setting` |
| audio → main | `report_status` | no | health beat (RSS, drop count, queue depths) |

**Reliability:**
- **Reconnect.** If the socket disconnects, audio retries with exponential backoff (200 ms → 5 s cap). First reconnect attempt is immediate (no backoff on the very first attempt after a disconnect).
- **Liveness.** Audio sends `report_status` to main every 5 s; main ack's. If audio gets no ack for 10 s, main is considered dead — any new `execute_tool` request fails fast as `tool_error` ("tool service unavailable") instead of waiting the full execute timeout. Bounds the dead-main worst case to ~10 s rather than ~30 s.
- **Per-tool execute timeout.** 30 s on `execute_tool` for the case where main IS alive but a tool is genuinely slow.
- **Startup race.** Audio boots with hardcoded sensible defaults for the settings it cares about (wake threshold, VAD thresholds, voice_mode). It does NOT block on main coming up. On first successful IPC connect, audio sends `request_setting` for each key and reconciles. Wake detection is live within a couple seconds of audio_process start regardless of main's state.
- **Server / client.** Main is the server; audio is the client. Systemd ordering should use sd-notify (main signals ready once the socket is listening) or systemd socket activation — `After=jarvis-node.service` alone doesn't guarantee the socket is bound before audio attempts the first connect.

**Why not MQTT-localhost / HTTP / gRPC:** Unix socket has zero auth surface (filesystem perms), no JSON-over-HTTP framing cost, no extra deps. MQTT adds ~5-15 ms per hop and another running service to depend on. HTTP-on-localhost works but requires a server framework. gRPC needs protoc + generated code we don't need yet.

## Code split

The voice_listener.py refactor already extracted these modules under `core/` — they migrate as-is to `audio_process/`:

```
jarvis-node-setup/
├── audio_process/                       ← NEW
│   ├── __init__.py
│   ├── main.py                          ← entrypoint
│   ├── ipc_client.py                    ← talks to main jarvis-node
│   ├── cc_client.py                     ← HTTP to CC (extracted from existing)
│   ├── wake_loop.py                     ← moved from core/
│   ├── wake_detector.py                 ← moved from core/
│   ├── wake_response.py                 ← moved from core/ (TTS playback carved out)
│   ├── wake_transcription.py            ← moved from core/
│   ├── wake_calibration.py              ← moved from core/
│   ├── follow_up_loop.py                ← moved from core/
│   ├── conversation_filters.py          ← moved from core/
│   ├── false_wake.py                    ← moved from core/
│   ├── vad_thresholds.py                ← moved from core/
│   ├── music_control.py                 ← moved from core/
│   ├── alert_announcer.py               ← moved from core/
│   ├── audio_bus.py                     ← moved from core/
│   ├── aec_pipeline.py                  ← moved from core/ (AEC is WIP, not abandoned;
│   │                                      stays put even though referenced as such
│   │                                      in older project notes)
│   ├── aec_calibrate.py                 ← moved from core/
│   ├── aec_reference.py                 ← moved from core/
│   ├── aec_speex.py                     ← moved from core/
│   │   (no tts_playback.py — aplay/pulse logic is already abstracted via
│   │    core/platform_audio.py, which is shared with main-process TTS providers
│   │    and command_execution_service. audio_process modules import back to
│   │    core.platform_audio. Discovered during Phase 2.)
│   └── tests/                           ← existing audio-module tests come along;
│                                          import-path sweep needed (`core.x` → `audio_process.x`)
│
├── scripts/
│   ├── main.py                          ← stays, drops wake-loop imports
│   └── voice_listener.py                ← retire (the 320 remaining lines move
│                                          to audio_process/ or get deleted)
│
├── services/
│   ├── ipc_server.py                    ← NEW: serves IPC requests from audio
│   ├── tool_executor.py                 ← stays in main
│   ├── alert_queue_service.py           ← stays; pushes via IPC
│   └── ... (settings, packages, agents) ← all stay
│
└── core/                                ← shrinks: only non-audio modules remain
```

**Shared state location:**
- `/home/pi/.jarvis/` — both processes read this. State is read-mostly per process (audio reads node_id/api_key on startup; main writes settings).
- `/home/pi/projects/jarvis-node-setup/.venv` — both processes share the same venv. Single `pip install` step; both entrypoints `python -m`.

**Settings reads:** audio process boots with hardcoded sensible defaults for the settings it cares about so wake detection comes up regardless of main's state. On first successful IPC connect, audio sends `request_setting` for each key and reconciles. Subsequent changes arrive via `set_setting` push from main when MQTT updates the DB. Audio never opens the encrypted DB directly.

## What the boundary breaks (and how to fix)

1. **AlertAnnouncer's queue probe.** `has_pending_high_priority_alerts()` currently reads `services/alert_queue_service` directly. Move that read to main; main pushes `play_alert` via IPC when one queues.

2. **Settings changes that affect audio.** Today MQTT push updates the DB and the in-process wake loop notices (or doesn't, depending on the setting). Now main must IPC `set_setting` to audio when relevant keys change.

3. **Factory reset / setup mode.** Today setup mode is a global flag. Now main IPCs `pause_audio_loop` before entering setup, `resume_audio_loop` after.

4. **Package install/uninstall that ships an `IJarvisCommand`.** Tools live in main process; no audio-side change needed. But if a package adds an *agent* that talks during quiet moments, that's an alert push (already covered).

5. **Tracing / log correlation.** Both processes log to `jarvis-logs`. Add a shared `audio_request_id` to IPC headers so the audio-side wake/STT/TTS spans correlate with the main-side tool execution spans in Loki.

## Phased plan

| Phase | Scope | Deliverable | Risk |
|---|---|---|---|
| **0** | This PRD. | Agreed scope. | — |
| **1 ✓ DONE** | Build IPC framing module + tests, both client and server, in-place. Tool execution still in-process. | `services/ipc_protocol.py`, `services/ipc_server.py`, `audio_process/ipc_client.py`, 66 tests (framing, concurrency, reconnect, liveness, integration). | Low — isolated. |
| **2 ✓ DONE (structural)** | Extracted 16 audio modules from `core/` to `audio_process/`. Added `audio_process/main.py` entrypoint + `JARVIS_AUDIO_PROCESS_MODE` env flag. When `split`, scripts/main.py skips wake-loop init + audio device setup + LLM warmup + sink keepalive and starts the IPC server; audio_process/main.py runs separately with its own bootstrap + IPC client + 5s heartbeat + control-plane handlers (`play_alert`, `pause_audio_loop`, `resume_audio_loop`, `set_setting`). Both modes exist side by side. **Tool execution still runs inside audio_process via the existing `CommandExecutionService` — Phase 2b moves it across the IPC boundary.** | 412 audio + IPC tests pass; broader test suite has 77 pre-existing failures unrelated to the refactor. | Medium — done. |
| **2b ✓ DONE** | Wired `execute_tool` IPC. Extracted `_execute_one_tool_locally()` + `_dispatch_one_tool()` in `utils/command_execution_service.py`; both `_execute_tools` (LLM tool loop) and `try_pre_route` (fast path) route through the dispatcher. Module-level `set_tool_runner_override(callable)` hook lets `audio_process/main.py` install an IPC runner that proxies to main's `_on_execute_tool` handler; main side strips `_on_response_complete` before serializing. `MSG_EXECUTE_TOOL` payload schema documented in `services/ipc_protocol.py`. | 14 new tests in `tests/test_command_execution_service_ipc.py` all pass; existing 19/23 in `test_command_execution_service.py` pass (4 pre-existing failures from `disabled_fast_paths` table + `send_command_unified` mock unchanged); 66 IPC tests + 330 audio/wake tests still green. command_discovery still in audio — that's Phase 2c. | Medium — done. |
| **2c (optional, deferred)** | Stop loading command modules in audio. Audio gets a thin command manifest via IPC for CC tool-list registration; main owns the registry + instances. | Audio RSS no longer scales with installed command count. | Medium-high — touches `command_discovery_service`, every caller that introspects commands, and the CC registration path. |
| **3 ✓ DONE (code) — soak pending** | File-based metrics instrumentation shipped on both sides. Added `subscriber_drop_count` + `get_queue_depths()` on AudioBus, `wake_loop_iters` + wake→ack latency buffer in `audio_process.wake_loop`, `get_pending_count()` + per-request latency samples on `IpcClient` (excludes heartbeat), and per-handler inflight/latency tracking on the main-side `_on_execute_tool`. New `services/metrics_writer.py` does atomic JSON writes with a periodic-writer thread helper. `audio_process/main.py` writes `/run/jarvis/audio_metrics.json` every 60 s; `scripts/main.py` writes `/run/jarvis/main_metrics.json` (split) or a combined row (in_process). All overridable via `JARVIS_AUDIO_METRICS_FILE` / `JARVIS_MAIN_METRICS_FILE`; opt out with `JARVIS_AUDIO_METRICS_DISABLED=1` / `JARVIS_MAIN_METRICS_DISABLED=1`. 20 new tests in `tests/test_metrics.py` pass; 133 tests total across Phase 2b + Phase 3 green. **Operator step:** 24 h baseline (in_process) + 24 h split run on jarvis-dev to compare metric files before Phase 4 install pattern. | Files + before/after report. Decision gate stays: proceed to Phase 4 or fix-and-loop. | Medium — done (code); soak is operator-driven. |
| **4** | Install pattern. Add `jarvis-audio.service` unit + install.sh `setup_audio_service()` + `refresh-services.sh` updates. Both services start under systemd. | Updated install.sh, second systemd unit template, rollback verified. | Medium — install path is fragile. |
| **5** | 48 h soak on jarvis-dev with systemd-managed both processes. | Soak report. | Low — should be a smaller delta from Phase 3. |
| **6** | Tag, ship to kitchen + living_room + bedroom. | Tagged release; kitchen upgraded first; rest follow if green for 48 h. | Low. |

## Phase 2b implementation brief (resumable from fresh context)

This section is the cold-start handoff for the next session. Everything Phase 1 + Phase 2 (structural) already shipped is on `feat/wake-audio-split`, uncommitted. The branch state is the source of truth — read it, don't re-derive.

### What's already in the branch (don't redo)

- 16 modules moved from `core/` to `audio_process/` (wake_*, follow_up_loop, conversation_filters, false_wake, vad_thresholds, music_control, alert_announcer, audio_bus, aec_*).
- 24 files had `from core.X` / `from core import X` rewritten to `audio_process.X`.
- IPC layer: `services/ipc_protocol.py`, `services/ipc_server.py`, `audio_process/ipc_client.py` (+ 4 test files, 66 tests).
- `audio_process/main.py` entrypoint: bootstrap (config, logging, audio volume, sink keepalive, LLM warmup), IPC client with heartbeat (`_build_status_payload`), control-plane handlers (`_on_play_alert`, `_on_pause_audio_loop`, `_on_resume_audio_loop`, `_on_set_setting`), then `start_voice_listener(DummyMusicAssistantService())`.
- `scripts/main.py`: `JARVIS_AUDIO_PROCESS_MODE` env flag at module top. Conditional voice_listener import. Four `if not _SPLIT_MODE:` guards (audio_volume, sink keepalive, LLM warmup, voice listener block). Module-level `_ipc_server` + `_start_ipc_server()` helper (currently only registers `MSG_REPORT_STATUS` → `MSG_STATUS_ACK`). `_handle_shutdown` stops the IPC server.
- PRD updated: `tts_playback.py` carve-out dropped (not needed), Q2/Q3/Q6 marked resolved, Q5 stale bedroom node carry-forward.

### What Phase 2b actually changes

Goal: per-tool `command.execute()` calls go from audio→main over IPC. Audio side stops running commands locally.

**File of interest:** `utils/command_execution_service.py` (~1215 lines). Two call sites to `command.execute()`:

- `_execute_tools(self, tool_calls, conversation_id, voice_command)` — main tool-loop, ~line 850–949. Iterates `tool_calls`, builds `RequestInformation`, calls `command.execute(request_info, secrets=_build_secrets(command), **arguments)` at L898, processes `CommandResponse` (`wait_for_input`, `clear_history`, `context_data["message"]`, `on_response_complete`, `error_details`).
- `_safe_pre_route` / pre-route path — fast-path command match before LLM, ~line 1020–1098. Same `command.execute()` shape at L1048. Lives inside the audio's voice flow; returns dict or None (None → falls through to LLM path).

**Design (recommended):**

1. **Extract `_execute_one_tool_locally(tool_call, voice_command, conversation_id) -> dict`** inside `CommandExecutionService`. Pure function (well, method) — takes the call, returns the per-tool result payload as a dict (`{success, wait_for_input, clear_history, tool_message, error_details, api_result, has_deferred_play}`). Refactor `_execute_tools` to call it in a loop and aggregate into `ToolExecutionResult`.
2. **Add module-level `_tool_runner_override` + `set_tool_runner_override(callable)`** in `utils/command_execution_service.py`. When set, `_execute_tools` calls the override instead of `_execute_one_tool_locally`. The override has the same input/output dict shape as the local helper.
3. **Audio side wiring:** in `audio_process/main.py`, after IPC client connects, call `set_tool_runner_override(lambda req: ipc_client.request(MSG_EXECUTE_TOOL, req, timeout=30).payload)`. Add `MSG_EXECUTE_TOOL` payload schema definition near other constants in `services/ipc_protocol.py` (already declared as a string constant; document the dict shape in a docstring).
4. **Main side wiring:** in `scripts/main.py`, `_start_ipc_server()` registers a new handler `MSG_EXECUTE_TOOL → _on_execute_tool`. `_on_execute_tool` builds a synthetic `ToolCall`-shaped object from the payload, instantiates a *local* `CommandExecutionService` (or holds a singleton at module level), calls `_execute_one_tool_locally`, returns `(MSG_TOOL_RESULT, payload, b"")`.
5. **Pre-route call site:** wire the same override path. Pre-route is audio-side already (fast path), but the `command.execute()` inside it needs to go via IPC too. Easiest: extract the inner `execute → CommandResponse → response-dict` into a helper that the override can replace. Or: gate just the `.execute()` call.

### Known limitations to call out in the implementation

- **`on_response_complete` callback can't cross IPC.** It's a Python callable (deferred-play trigger for music). Phase 2b: drop it on the audio side (log a warning when a response carries it). Deferred-play features (music start-after-TTS) regress slightly until a `play_now` IPC push message is added (Phase 2b.x or 2c).
- **`_maybe_take_over_music(command, arguments)`** runs *before* `command.execute()` in both call sites. It stops sibling music players. Today this happens in the same process as the command. In split mode it should run on the main side too (move it into `_execute_one_tool_locally`). Verify by inspection.
- **`_build_secrets(command)`** is called per-tool to assemble the secrets dict. Lives in audio today but reads from the encrypted store. Main owns the encrypted store. Move it to main side (the IPC handler builds secrets from main's view).
- **`set_current_user_id(user_id)`** is a contextvar from `jarvis_command_sdk.context` set before `.execute()` so per-user-scoped data flows correctly. Must be set on the main side around the execute call, not audio.

### Tests

- `tests/test_command_execution_service.py` exists (has 4 pre-existing failures noted in Phase 2 verification). Keep changes mock-compatible — most tests mock `command_discovery` and the CC client.
- Add a new `tests/test_command_execution_service_ipc.py` for the override hook: stub `_tool_runner_override`, run `_execute_tools` with a single tool_call, verify the override is invoked with the right payload and the result aggregates correctly.
- No need to add an end-to-end "real IPC" test here — the IPC layer is already tested in Phase 1.

### Verification

Same DoD as the rest of the rework: user runs both processes on `jarvis-dev.local` (two tmux windows per the soak section below) and confirms a voice command with tool calls round-trips cleanly. Then a tagged release with updated `install.sh` deploys cleanly.

### Where to look first when resuming

1. Read this brief.
2. Read `utils/command_execution_service.py` L850–950 (main tool loop) and L1020–1098 (pre-route).
3. Read `audio_process/main.py` (`_start_ipc_client` + the handler hooks already there).
4. Read `scripts/main.py` `_start_ipc_server` (currently only handles `MSG_REPORT_STATUS`).
5. Start with step 1 above (extract `_execute_one_tool_locally`). Then 2 (override hook). Then 4 (main-side handler). Then 3 (audio-side wiring). Then 5 (pre-route).

## Test pattern on jarvis-dev.local

### Run-in-place iteration loop (Phases 2-3)

This is the inner loop. No systemd touched. `scp` the new modules over, run the audio_process by hand, watch logs.

```bash
# On laptop:
cd jarvis-node-setup
./sync_files_to_zero.sh        # existing — pushes code to /opt/jarvis-node
ssh pi@jarvis-dev.local 'sudo systemctl stop jarvis-node'

# In tmux/screen on the Pi:
# Window 1: main process (no wake loop)
cd /opt/jarvis-node
JARVIS_AUDIO_PROCESS_MODE=split .venv/bin/python -m scripts.main

# Window 2: audio process
cd /opt/jarvis-node
.venv/bin/python -m audio_process.main

# Window 3: tail logs
journalctl -f -u jarvis-node              # nothing — service is stopped
sudo tail -f /var/log/messages | grep -iE "wake|audio|drop"

# Validate:
# - say "hey jarvis" → chime + ack within ~1 s
# - "what's the weather" → tool flow exercises IPC
# - kill -9 the main process; audio survives, next wake gets "tool service unavailable"
# - restart main; audio reconnects, wake works again
```

### A/B measurement: file-based metrics

Each process writes its own metrics file every 60 s. The soak script reads those files — no journalctl grepping, no log-level dependency, no fragile log-line matching.

**Writers (shipped — see `services/metrics_writer.py`):**
- Audio process writes `/run/jarvis/audio_metrics.json` every 60 s: `{ts, rss_kb, subscriber_drop_count, wake_loop_iters, queue_depths: {wake, follow_up}, last_wake_to_ack_ms_p50, last_wake_to_ack_ms_p95}`. Built by `audio_process.main._build_audio_metrics`. Path override: `JARVIS_AUDIO_METRICS_FILE`. Opt out with `JARVIS_AUDIO_METRICS_DISABLED=1`.
- Main process writes `/run/jarvis/main_metrics.json` every 60 s: `{ts, rss_kb, ipc_inflight, ipc_p95_latency_ms, audio_liveness_ok}`. Built by `scripts.main._build_main_metrics`. Path override: `JARVIS_MAIN_METRICS_FILE`. Opt out with `JARVIS_MAIN_METRICS_DISABLED=1`.
- In single-process baseline mode (`JARVIS_AUDIO_PROCESS_MODE=in_process`), main writes a combined file that includes the audio counters (since the audio code is still in-process). Built by `_build_combined_metrics`. `audio_rss_kb` is omitted in this mode (it'd duplicate `rss_kb`).
- Counters (`subscriber_drop_count`, `wake_loop_iters`) are monotonic from process start; the soak script diffs across reads for per-interval rates.
- p50/p95 are computed from a rolling window of the last 100 samples (wake→ack on the audio side, per-handler on the main side) — the percentile reflects "recent" behavior, not the lifetime of the process. Heartbeat IPC requests are excluded from the audio-side IPC latency buffer so the percentile reflects real tool work.

**Soak script** (runs on the Pi, appends one row per minute):

```bash
#!/bin/bash
mkdir -p /tmp/jarvis_soak
while true; do
  ts=$(date -u +%s)
  audio=$(cat /run/jarvis/audio_metrics.json 2>/dev/null || echo '{}')
  main=$(cat /run/jarvis/main_metrics.json  2>/dev/null || echo '{}')
  echo "$ts $audio" >> /tmp/jarvis_soak/audio.jsonl
  echo "$ts $main"  >> /tmp/jarvis_soak/main.jsonl
  sleep 60
done
```

Baseline: 24 h single-process.
Split: 24 h split mode.
Compare. Looking for: audio-process RSS flat or within ±10% of baseline's wake budget; subscriber-drop rate falls to single digits per minute.

### Install-pattern test (Phase 4)

```bash
# On laptop:
git checkout feat/wake-audio-split
git tag v0.1.X-dev
./scripts/release.sh                       # if it exists

# On jarvis-dev:
ssh pi@jarvis-dev.local
sudo systemctl stop jarvis-node
curl -L <release tarball> | tar xz -C /tmp
cd /tmp/jarvis-node-setup-*
sudo bash install.sh                       # exercises new setup_audio_service()
systemctl status jarvis-node jarvis-audio  # both should be active (running)
journalctl -u jarvis-audio -n 200 --no-pager
```

The existing install.sh `.bak` rollback (auto-rolls back if new service doesn't go active in 120 s) needs to cover BOTH units. Verify by deliberately breaking the audio unit template and confirming a clean rollback to old root-mode single-process state.

## Rollback

- **Phase 2-3 (no install changes):** revert to single-process by setting `JARVIS_AUDIO_PROCESS_MODE=in_process` (default) and restarting jarvis-node. The audio_process/ code lies dormant.
- **Phase 4-5 (split installed):** revert by removing `/etc/systemd/system/jarvis-audio.service`, disabling the unit, setting the env var back to `in_process` in jarvis-node's unit file, restarting. The Phase 2 dual-mode flag is what makes the rollback safe.
- **Phase 6 (prod nodes):** standard tagged-release rollback per existing kitchen-deploy practice; pin previous version.

The dual-mode env var is the load-bearing safety net. Don't delete it until at least 30 days of clean production soak.

## Open questions

1. **Audio process owning CC node-creds.** Today only one process holds them. Both reading from `/home/pi/.jarvis/` is fine; do we want the audio process to refresh K2 itself, or have main do it and push? Recommend: main owns K2 lifecycle, audio reads at startup + on IPC `set_setting` for k2-related keys. Audio doesn't write to the encrypted store.

2. **Tool execution timeout vs CC timeout. RESOLVED — split liveness from per-tool execute.** Audio tracks main's liveness via a 5 s `report_status` heartbeat. If audio gets no ack for 10 s, main is dead → new `execute_tool` calls fail immediately. The 30 s timeout stays for the case where main IS alive but a tool is genuinely slow. Bounds dead-main worst case to ~10 s rather than ~30 s + reconnect backoff. See the updated reliability section.

3. **Memory budget for audio process.** Target: ~200-250 MB RSS steady state. Residents: openWakeWord (~50 MB), pyaudio (~20 MB), Python interpreter (~30 MB), AEC pipeline + speex + reference store (~50 MB incl. model state), our code (~30 MB). Headroom for AudioBus buffers + TTS audio chunks. If we blow past 300 MB the split hasn't helped; need to investigate per-module sizes before committing.

4. **Two-process aarch64 GIL contention.** Each process has its own GIL. Not a problem; net positive vs single-process where the wake thread fights the agent thread for the GIL.

5. **The 4th bedroom node (55e2b570, v0.1.94).** Stale by ~13 versions. Likely needs a baseline update before the split work even matters there.

6. **AEC. RESOLVED — AEC is WIP, not abandoned.** Despite the `project_aec_audio_routing.md` memo, `core/aec_pipeline.py`, `aec_calibrate.py`, `aec_reference.py`, `aec_speex.py` are live and imported by `wake_loop.py:30` and `voice_listener.py:30`. All four move into `audio_process/` with the rest of the stack. Memory budget adjusted in Q3 to include them. Decision: do NOT gut AEC; leave the WIP in place and migrate as-is.

## Definition of done

- [ ] Phase 1-5 deliverables shipped per table above
- [ ] **Manual end-to-end verification on `jarvis-dev.local`**: voice command with tool calls round-trips cleanly in split mode; user confirms it works.
- [ ] **Tagged release with updated `install.sh` deploys cleanly on `jarvis-dev.local`**: both services come up active; rollback rehearsed by deliberately breaking the audio unit template and confirming the `.bak` restore brings the old single-process state back.
- [ ] Soak measurements collected for the record (24 h baseline + 24 h split, file-based) and appended to this PRD — data for the kitchen rollout decision, not a pass/fail gate.
