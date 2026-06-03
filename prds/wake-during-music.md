# Wake-during-music (stereo-mic + multichannel echo cancellation)

**Status:** draft, feat/wake-during-music

## Problem

"Hey Jarvis" fails to fire when music plays through the same speaker the node hears. Today's prod kitchen journal (v0.1.100, AEC disabled) over ~30 min of Spotify playback: **18 `wake-suppressed-music-bleed` events vs 4 `Wake fired`** (2 of those via the 0.95 trust-score bypass). Reported in the wild as "Hey Jarvis just doesn't work while music is playing — I have to turn the volume down to talk to it."

Today's investigation ruled out three would-be fixes:

- **Speex AEC (commit 47ce7b7, `aec_enabled=False` default)** — enabled on dev and exercised against Spotify. Startup calibration consistently failed (`too few mic samples collected | collected=7680, target=105600` — got exactly one AudioBus chunk before queue-empty timeout). Falling back to the static 80 ms delay, Speex's adaptive NLMS filter could not converge against music transients: suppression bounced between -2 and +4 dB, with one momentary 7.8 dB spike that decayed within seconds. The net effect was wake scores no better than with AEC off, and occasionally worse.
- **Lower the `wake_music_trust_score` bypass from 0.95 → 0.75** — applied to dev and tested. At loud playback (baseline_rms 11k-13k) OWW scores capped at 0.14-0.43 because the model can barely see "Hey Jarvis" through the bleed. No trust-score value rescues a 0.4 score that needs to be 0.5. The change is theater for the loud-music regime.
- **Lower the mic capture PGA from 60 % (+35.5 dB, install.sh default) to 49 % (+29 dB) and 42 % (+25 dB)** — captured stereo recordings via `parec` and found the ADC was **digitally clipping** at the default gain (peak 0.00 dB, flat factor 13.9, bit-depth 16/16 with 634 saturated samples on the right channel in a 15-second window). Dropping the gain eliminated clipping and got a few legitimate wakes to fire — but exposed a new failure mode: with the mic gain low enough that music doesn't saturate the ADC, music itself starts to **pattern-match the wake phrase**. Multiple "wake fired" events at 0.95+ scores with `pre_wake_speech_seconds: 0.0` (no voice activity in the 5-second pre-wake window) — music-only false-wakes. Each false-wake triggers a CC roundtrip → returns `not_for_me` → escalates to a 60 s cooldown that blocks real user attempts. PGA is a tradeoff dial, not a fix: high values clip real voice, low values let music spoof the wake.

The bottleneck is the OWW input — not the gate, not the trust-score, not the gain. The model needs cleaner audio than a single bleed-saturated mic can provide, *and* a way to tell user-voice apart from music that pattern-matches the wake phrase.

## Hardware available but underused

ReSpeaker 2-Mics Pi HAT v2.0 (TLV320AIC3104) has **two microphones**, ~30 mm apart. Today the node captures via `dsnoopmic`, a single-channel ALSA device. The second mic is wired but unused.

A 30 mm baseline is short relative to typical room dimensions (speaker ~500 mm+), so the speaker bleed arrives at both mics nearly identically. **Simple L−R subtraction won't help.** What it does enable:

1. **Stereo input to a proper AEC** (WebRTC AEC3) — uses both mics + the PA monitor reference as a 3-channel solve. Industry standard for smart-speaker echo cancellation.
2. **Blind source separation (ICA / NMF)** — geometry-agnostic; can separate independent sources even with small spatial difference. Heavier CPU.
3. **Beamforming with null steering** — calibrate a null toward the speaker. Cheap CPU; brittle to setup geometry changes.

The PRD takes Option 1 as primary, with Option 2 as fallback if AEC3 doesn't work on Pi Zero 2W.

## Success criteria

> At Spotify normal kitchen listening volume, "Hey Jarvis" fires within 1 wake-attempt cycle with **≥90% reliability**, with **no more than 1 music-only false wake per hour** of continuous playback.

Measured against a fixed test corpus:
- 20 wake utterances spoken at the dev Pi at 1.5 m from the node, recorded across 3 music genres (vocal pop, percussive electronic, ambient instrumental) at 2 volume levels (moderate ~baseline_rms 2-3k, loud ~baseline_rms 8-12k).
- 60 minutes of continuous music in each genre/volume without intentional wake attempts → false-positive count.

Off-ramp: if CPU on Pi Zero 2W can't sustain real-time stereo AEC3 + OWW at 16 kHz, fall back to ICA-based separation (lighter CPU, less convergent) or accept "moderate volume only" success.

## Phased plan

| Phase | Scope | Deliverable | Risk |
|---|---|---|---|
| **0** | This PRD | Agreed plan, success metric, off-ramps | — |
| **1** | Diagnostic recordings on the dev Pi via `parec` against the existing PA source — no node code change. Three takes: voice-only, music-only, voice+music. Recorded at PGA setting that doesn't clip (~49 % / +29 dB). Each take saved as stereo s16le 48 kHz raw + WAV. For takes 2 and 3, simultaneous PA monitor reference capture. | WAVs in `prds/wake-during-music/recordings/` (or external storage if too large for git); per-take notes on conditions. | Music volume and material affects analysis — keep representative samples. |
| **2** | Offline analysis: inter-channel coherence, voice-vs-music spatial signature, reverb tail length, peak / RMS distributions, mic-2 gain match. Pick the algorithm class (AEC3 vs ICA vs beamforming) based on what the data actually shows, not what the PRD assumes. | `prds/wake-during-music/analysis.md` with the algorithm decision + reasoning. | Decision may be "data shows no algorithm is going to clear the bar on this hardware" → re-scope. |
| **3** | Combined AudioBus stereo refactor + algorithm implementation. AudioBus exposes a new `subscribe_stereo()` API so existing mono consumers stay unchanged; the new wake-front-end consumes stereo + PA monitor reference. Wrapper module under `core/wake_clean_*.py`. CPU benchmark on real Pi Zero 2W. | Working `wake_clean.process(stereo, ref) → mono` on dev hardware. Tests against the Phase-1 corpus. | CPU budget. Algorithm tuning. ALSA / dsnoopmic interactions with PA holding the card. |
| **4** | Wire cleaned mono into wake path; retire Speex AEC + music-mode energy gate + trust-score bypass (all become redundant once the OWW input is clean). Also: settle on the correct PGA default for the new front-end (probably lower than today's 60 %, since AEC is doing the heavy lifting and we want headroom). | Single code path; old AEC modules + music-mode gate removed; install.sh PGA default updated. | Regression on non-music wake reliability — validate via Phase 5. |
| **5** | Validation against the success-criteria corpus. A/B comparison vs main (single-mic, default PGA) baseline at matching volumes. Run for ≥1 week on the dev Pi. | Ship/no-ship report. If ship: tag, GitHub pre-release, deploy to dev Pi for a week, then promote. | Reveals success metric is unrealistic on this hardware. |

Each phase ends with a checkpoint commit on this branch. Phase boundaries are review points — easy to pause/redirect without sunk-cost pressure.

## Risks and unknowns

- **CPU on Pi Zero 2W.** Current load: OWW predict at 60-80 ms per 80 ms chunk (right at real-time). Adding stereo AEC3 may push past real-time → wake chunks dropped. Mitigation: profile each phase on actual hardware before committing to the algorithm.
- **Mic-2 quality / wiring.** Today's `dsnoopmic` may be masking a hardware issue with the second mic. Phase 1 capture has to verify both channels carry signal. (Initial diagnostic on dev: peaks at -27.0 / -28.2 dB and crest factors 8.50 / 7.36 — both mics carrying signal with the slight difference expected from two physical transducers, not a duplicated mono source.)
- **PGA tradeoff isn't going away.** Lower PGA fixes clipping but invites music-spoof false-wakes; higher PGA prevents music-spoof but kills real voice via clipping. The new front-end has to work at whichever PGA we ship — likely lower than today's 60 % default, but the algorithm has to be robust enough that we're not just sliding the failure mode around.
- **WebRTC AEC3 Python binding.** No mature pip package; will use ctypes against `libwebrtc-audio-processing-1-3` (already apt-installed by `install.sh`). API surface is sizeable. Risk of stale ABI vs distro version.
- **Music transients defeat AEC convergence.** Tested today with Speex; same risk applies to AEC3, just at a higher quality bar. AEC3's residual echo suppressor is designed exactly for this case but is not a silver bullet.
- **`not_for_me` cooldown amplifies any false-wake misstep.** Today's PGA experiment showed: one false-wake during music → CC roundtrip → `not_for_me` → 20 s cooldown; two in 30 s → 60 s escalated cooldown. Whatever algorithm we ship, if its false-wake rate is non-zero during sustained music, the cooldown logic compounds the user-visible failure ("Hey Jarvis just stops working for a minute"). The success metric explicitly bounds music-only false wakes to ≤ 1/hour for this reason.
- **Calibration / setup ergonomics.** Beamforming needs speaker-position knowledge. If we adopt it as a fallback, we need a calibration routine that doesn't require user intervention.

## Out of scope

- Retraining the OWW model on music-mixed data (separate, longer effort).
- Improving wake during non-music noise (vacuum, dishwasher, conversation) — same techniques may transfer, but the success metric here is music-specific.
- Multi-room beamforming or directional wake (which mic is closer to the speaker).
- Replacing ReSpeaker hardware.

## Release strategy

All work lands on `feat/wake-during-music`. Per-phase commits, no main merges until Phase 5 ships. To test on the dev Pi during Phases 3-5:

1. Tag a release off the branch: `v0.1.X-aec.N` (numbered iterations).
2. Push the tag to GitHub and mark the release as a **pre-release** — prod nodes that pull `releases/latest` skip pre-releases.
3. On the dev Pi, run `install.sh` with the tag pinned: `TAG=v0.1.X-aec.N curl ... | sudo bash` (or modify install.sh to honor a `--tag` flag if needed).
4. After validation, either merge to main + tag a real release, or iterate on the branch.

## Reference

- Today's investigation log with all raw evidence: [`wake-during-music/findings-2026-06-03.md`](wake-during-music/findings-2026-06-03.md). Dev Pi journal 2026-06-03 15:32-17:00 (AEC enabled→disabled, trust-score 0.95→0.75→reverted, PGA 71→58→50→58, music false-wakes confirmed via `pre_wake_speech_seconds: 0.0` on `Wake fired` events at high scores).
- Existing AEC code: `core/aec_speex.py`, `core/aec_reference.py`, `core/aec_pipeline.py`, `core/aec_calibrate.py` — will be removed in Phase 4.
- Existing wake gate: `scripts/voice_listener.py:2060-2200` (music_mode, trust_score, energy_floor) — will be removed in Phase 4.
- PGA default: `install.sh:751` (`amixer -c seeed2micvoicec sset 'PGA' '60%'`) — will be re-tuned in Phase 4 once the algorithm decides the right headroom.
- `not_for_me` cooldown: `core/voice_filters.py:93,140-165` — escalates from 20 s to 60 s after 2 hits in 30 s.
- ReSpeaker hardware notes: `~/.claude/projects/-Users-alexanderberardi-jarvis/memory/project_respeaker_hat.md`.
