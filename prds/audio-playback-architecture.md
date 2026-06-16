# Audio playback architecture rework — replace the v0.1.100 keepalive

**Status:** draft, 2026-06-03 EOD. Branch: feat/wake-during-music. Companion to wake-during-music.md — different problem, different bottleneck.

## Problem

`jarvis-node-setup` v0.1.100 shipped a continuous silent `paplay /dev/zero` (the "sink keepalive") on the default sink to work around a TLV320AIC3104 driver bug where pulse cannot resume the sink from `SUSPENDED` ("Resume failed, couldn't restore original sample settings" floods journald). With the keepalive in place, the sink stays in `RUNNING` and TTS / music / chimes all play reliably for the first time since v0.1.94.

**But the keepalive added a regression we can't accept**: any time another sink-input is on the same sink (music, TTS, chimes), audible static appears on loud passages. End-of-day user report on dev: "actually sounds horrible." Verified at every Line-gain tuning we tried (+4 dB → +1 dB) and at every keepalive sink-input volume we tried (100 % → -169 dB). The trigger is *the existence of a second active sink-input on the real sink*, not its volume contribution, sample rate, or codec gain.

Killing the keepalive entirely returns to clean audio — but also returns the sink-wedge bug. We can't ship either state to the kitchen node.

## Today's investigation — what we ruled out

1. **Lower keepalive volume** (paplay `--volume=1` → pactl `set-sink-input-volume … 100` → -169 dB effective). Still static. Pulse-mixer-headroom theory falsified.
2. **Match Spotify's native rate** (48 kHz → 44.1 kHz keepalive). Sink ended up at 48 kHz anyway (pulse default-sample-rate); keepalive resamples 44.1→48, no measurable change to the static. Resampler-artifact theory falsified for keepalive specifically — music still gets resampled either way.
3. **Drop codec Line analog gain** (+4 dB → +2 dB → +1 dB). +2 dB reduced static modestly (committed as the new install.sh / self-heal baseline, `fb97212` on feat/wake-during-music). +1 dB sounded worse — likely codec analog stage in a worse drive regime, or user crank-up to compensate pushed pulse digital path closer to clip. Codec headroom is real but not the dominant factor.
4. **Disabling `module-suspend-on-idle`** (already shipped via `/etc/pulse/default.pa.d/99-jarvis-no-suspend.pa`). Sink still goes `SUSPENDED` through some other path — confirmed by direct observation today on dev. So the wedge isn't only autosuspend; there's another suspend trigger we haven't identified.
5. **`pactl suspend-sink … 0` to wake a SUSPENDED sink**. Returns `"Invalid argument"` on this hardware; clears only the `SUSPEND_USER` cause bit and the TLV320 driver has other cause bits set. Documented in `utils/audio_volume.py::reload_alsa_card_if_suspended`.

## What's load-bearing today (v0.1.100, do not regress)

- The `_play_streaming_audio` generator-with-idle-timeout (`_audio_chunks` in `utils/command_execution_service.py`) — fixed a real aplay leak. Keep regardless of how we replace the keepalive.
- `play_audio_file`'s 60 s hard timeout on aplay. Keep.
- The defensive stale-`_playback_proc` kill in `play_pcm_stream`. Keep.
- Streaming-continue forwards `on_response_complete`. Keep (was the music-not-playing bug).
- `_pause_active_playback` two-class split (SIGSTOP-bound move-to-null vs mute-only). Keep — fixed both the SIGSTOP-induced wedge and the librespot-HTTP-hangs-after-SIGCONT bug.
- `wake_music_trust_score = 0.95` bypass on the music-bleed gate. Keep (separate concern, see wake-during-music PRD).
- `force_reload_alsa_card()` in `utils/audio_volume.py` + the call from `_restore_then_complete`. Keep as the wedge fallback if the new mechanism is event-driven rather than continuous.

## What's the actual root cause we still don't understand

When the real ALSA sink is in `SUSPENDED` and a new sink-input is attached, pulse logs `Resume failed, couldn't restore original sample settings` indefinitely and the sink-input gets no audio. The TLV320 driver's resume path is broken. The keepalive papers over this by never letting the sink go to `SUSPENDED` in the first place — but introduces the multi-stream-static problem.

What we **don't** know:
- Exact pulse code path triggering the SUSPEND when `module-suspend-on-idle` is disabled. Could be `module-card-restore` restoring a previously-saved suspended state, or pulse's internal "no streams for N seconds, close ALSA device" path, or something in the alsa-card profile negotiation.
- Whether the TLV320 driver bug is in the kernel mainline TLV320AIC3X driver (`sound/soc/codecs/tlv320aic3x.c`), the Pi BCM2835 I2S glue, or the Seeed-published overlay we vendor at `setup/respeaker-2mic-v2_0-overlay.dts`. Have not bisected.
- Whether the same SUSPEND can be observed with `aplay -D plughw:1,0` directly (bypassing pulse) — if not, pulse is the suspending agent and we have config-level options. If yes, it's kernel and we need a driver-level workaround.
- Whether running pulse with `default-fragment-size-msec` at the default (~25 ms) instead of the install.sh-set 15 ms changes the multi-stream-static behavior. 15 ms is unusual.

## Possible directions (none yet proven)

| Approach | Sketch | Risk |
|---|---|---|
| **A. ALSA-direct keepalive bypassing pulse** | `aplay -D plughw:1,0 < /dev/zero` claims the underlying PCM device at the kernel level. Pulse loses exclusive ALSA access — likely breaks pulse's `module-alsa-sink` entirely. Probably won't work as drop-in. | High — fundamentally conflicts with pulse architecture. |
| **B. Pulse `module-loopback` from `jarvis_duck_null.monitor` to real sink** | Pulse-internal stream rather than external paplay client. Pulse may handle module-owned sink-inputs differently in the mixer path. Still creates a sink-input on the real sink → may have identical multi-stream-static behavior. | Medium — untested. Cheap to try. |
| **C. Identify and disable the actual SUSPEND trigger** | If the suspend is from `module-card-restore` reading a stale tdb, deleting/rewriting the tdb on every boot may prevent it. If it's pulse's "no streams idle close", find the relevant `module-alsa-card` arg. Eliminates the need for any keepalive. | Medium — requires actual debugging of pulse internals. Best long-term outcome if successful. |
| **D. Driver-level fix for TLV320 resume** | Investigate `sound/soc/codecs/tlv320aic3x.c` resume path. Possibly a kernel patch or DT property. Out of scope for this repo but durable. | Very high effort. Right answer if hardware is the actual culprit. |
| **E. Event-driven recovery without keepalive** | No continuous stream. Use pulse subscribe (`pactl subscribe`) to watch for sink-state changes and reload `module-alsa-card` the instant any sink goes SUSPENDED. Single sink-input at a time → no multi-stream static. Wedge window is the few seconds between SUSPEND and reload. May still glitch the first audio after wedge. | Medium — needs a daemon (or python listener) running. CPU/memory cost. |
| **F. Accept the trade-off** | Ship v0.1.100 + Line=2 baseline. Static at loud peaks, but reliable playback. Document max-comfortable-volume guidance. | Low effort. Worst UX. Probably not acceptable per user report. |

Current author lean: **C first** (cheapest win if it pans out), then **B**, then **E**, then **D**. Run **F** as the temporary safety net while C is investigated.

## Success criteria

> 30 minutes of mixed playback (Spotify + voice commands + Pandora) at the volume the user reports as "loud kitchen listening level" on <kitchen-node>.local **without audible static** AND **without the sink wedging**. Validated over a 24-hour soak test on <dev-node>.local before kitchen deploy.

Measured by:
- Subjective listening at the dev Pi at a calibrated volume level. Pass = no static perceptible to user.
- Journal scan for `Recovering wedged audio sink` events — expect 0 over 24 h once the new mechanism is in place.
- `pulseaudio` journal scan for `Resume failed` — expect 0 over 24 h.
- Spotify-specific: `~/.jarvis/spotify/go-librespot.log` for `did not receive last pong from dealer` — should not appear (today's morning prod incident showed this happens when alsa-card reloads disrupt librespot's pulse connection).

## Phased plan

| Phase | Scope | Deliverable | Risk |
|---|---|---|---|
| **0** | This PRD. Restore dev to a workable state at session start. | Agreed plan + dev usable. | — |
| **1** | Reproduce SUSPEND deterministically on dev with the keepalive removed. Instrument: which pulse module logs first when sink goes SUSPENDED? `pulseaudio --log-level=debug` during a sink idle period. Cross-check kernel `/proc/asound/seeed2micvoicec/pcm0p/sub0/status` for PCM state machine. | Logged evidence pinpointing the suspend agent. | May not be reproducible on demand. May need to wait for natural occurrence. |
| **2** | Based on Phase 1 — try option C (config-level disable) first. Touch one knob at a time, measure with the success-criteria methodology each time. If C fails, try B. If B fails, try E. | A working mechanism with no static + no wedge over 1 h smoke test. | The right fix may require multiple knobs. |
| **3** | Once Phase 2 lands, retire the keepalive code in `scripts/main.py`. Keep `force_reload_alsa_card` as the last-resort safety net but expect it never to fire. Add a heartbeat log if it does fire so we can see whether Phase 2 is actually working in the wild. | Cleaner `main.py`. Telemetry on residual wedges. | None significant. |
| **4** | 24-hour soak on dev with mixed-volume playback + voice commands. Compare static and wedge counts vs v0.1.100. | Soak report; ship-or-revisit decision. | Reveals the success criteria needed tightening. |
| **5** | Deploy to kitchen as v0.1.101. The new alsa-card stays at Line=2 from `fb97212` since that change is independent of the keepalive mechanism and gives modest extra headroom. | Tagged release, kitchen upgraded. | Kitchen has its own hardware quirks that may differ from dev. |

## Open items to capture for the next session

- **Uncommitted on `scripts/main.py`** (feat/wake-during-music): the auto-volume-drop code for the keepalive sink-input — should be reverted as part of Phase 0 since the keepalive itself is going away.
- **`fb97212` (Line=2 baseline)** lives on feat/wake-during-music only. If the keepalive replacement gets cherry-picked to main as part of a v0.1.101, take `fb97212` with it. The drift floor change to `_TLV320_LINE_MIN_VALUE = 1` matters — without it the self-heal on existing v0.1.99 / v0.1.100 nodes won't accept the new baseline.
- **Spotify dealer-pong recovery** is a separate follow-up flagged in `project_audio_architecture.md`: when go-librespot loses sync with Spotify's dealer service (60 s of missed pongs), HTTP control endpoints hang and the only fix today is restarting jarvis-node. Worth handling in the `spotify_keepalive` agent independently. Not blocked by this PRD.
- **`module-suspend-on-idle` pa.d drop-in** stays in place even if we identify a better mechanism in Phase 2 — defense in depth.

## Hardware / config snapshot at PRD time

- jarvis-node-setup main: **v0.1.100** (`db4bb2d`)
- feat/wake-during-music tip: **`fb97212`** (this branch's Line=2 change on top of PRDs)
- Pi Zero 2 W + ReSpeaker 2-Mics HAT v2.0 + TLV320AIC3104
- Codec mixer (post-`fb97212`): PCM 100 %, Line +2 dB, Line DAC -1.5 dB, HP/HPCOM muted
- PulseAudio 17, `flat-volumes = no`, `default-fragment-size-msec = 15`, `default.pa.d/99-jarvis-no-suspend.pa` unloads `module-suspend-on-idle`
- Sink default-sample-rate left at pulse default (44100); sink runs at 48000 when alsa-card loads — pulse uses negotiated rate per stream
- go-librespot 0.7.2, localhost:3678 HTTP API
