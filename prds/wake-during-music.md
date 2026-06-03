# Wake-during-music (stereo-mic + multichannel echo cancellation)

**Status:** draft, feat/wake-during-music

## Problem

"Hey Jarvis" fails to fire when music plays through the same speaker the node hears. Today's prod kitchen journal (v0.1.100, AEC disabled) over ~30 min of Spotify playback: **18 `wake-suppressed-music-bleed` events vs 4 `Wake fired`** (2 of those via the 0.95 trust-score bypass). Reported in the wild as "Hey Jarvis just doesn't work while music is playing — I have to turn the volume down to talk to it."

Today's investigation ruled out two would-be fixes:

- **Speex AEC (commit 47ce7b7, `aec_enabled=False` default)** — enabled on dev and exercised against Spotify. Startup calibration consistently failed (`too few mic samples collected | collected=7680, target=105600` — got exactly one AudioBus chunk before queue-empty timeout). Falling back to the static 80 ms delay, Speex's adaptive NLMS filter could not converge against music transients: suppression bounced between -2 and +4 dB, with one momentary 7.8 dB spike that decayed within seconds. The net effect was wake scores no better than with AEC off, and occasionally worse.
- **Lower the `wake_music_trust_score` bypass from 0.95 → 0.75** — applied to dev and tested. At loud playback (baseline_rms 11k-13k) OWW scores capped at 0.14-0.43 because the model can barely see "Hey Jarvis" through the bleed. No trust-score value rescues a 0.4 score that needs to be 0.5. The change is theater for the loud-music regime.

The bottleneck is the OWW input — not the gate. The model needs cleaner audio than a single bleed-saturated mic can provide.

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
| **1** | Stereo capture refactor — ALSA config to stereo, AudioBus 2-channel publish. Downstream still uses channel 0 only (zero behavior change). | Stereo audio reaches AudioBus subscribers; existing wake/STT paths unaffected | Mic-2 wiring on HAT, ALSA `dsnoopmic` rewrite |
| **2** | Diagnostic recordings (voice-only, music-only, voice+music) + analysis. Quantify inter-channel coherence, voice/music spatial signatures, room reverb tail. | Decision doc: AEC3 / ICA / beamforming choice. Saved WAVs for later regression testing. | Decision may be "neither works well enough" |
| **3** | Implement chosen algorithm via ctypes (WebRTC) or numpy/scipy (ICA). Wrapper module under `core/wake_clean_*.py`. CPU benchmark on actual Pi Zero 2W. | `wake_clean.process(stereo, ref) → mono` API. Tests against Phase-2 corpus. | CPU budget. Algorithm tuning. |
| **4** | Wire cleaned mono into wake path; retire Speex AEC + music-mode energy gate (the gate becomes redundant once the OWW input is clean). | Single code path; old AEC modules deleted. | Regression on non-music wake reliability |
| **5** | Validation against the success-criteria corpus. A/B comparison vs main (single-mic) baseline at matching volumes. | Ship/no-ship report. If ship: tag, GitHub pre-release, deploy to dev Pi for a week, then promote. | Reveals success metric is unrealistic on this hardware |

Each phase ends with a checkpoint commit on this branch. Phase boundaries are review points — easy to pause/redirect without sunk-cost pressure.

## Risks and unknowns

- **CPU on Pi Zero 2W.** Current load: OWW predict at 60-80 ms per 80 ms chunk (right at real-time). Adding stereo AEC3 may push past real-time → wake chunks dropped. Mitigation: profile each phase on actual hardware before committing to the algorithm.
- **Mic-2 quality / wiring.** Today's `dsnoopmic` may be masking a hardware issue with the second mic. Phase 1 capture has to verify both channels carry signal.
- **WebRTC AEC3 Python binding.** No mature pip package; will use ctypes against `libwebrtc-audio-processing-1-3` (already apt-installed by `install.sh`). API surface is sizeable. Risk of stale ABI vs distro version.
- **Music transients defeat AEC convergence.** Tested today with Speex; same risk applies to AEC3, just at a higher quality bar. AEC3's residual echo suppressor is designed exactly for this case but is not a silver bullet.
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

- Today's investigation: dev Pi journal 2026-06-03 15:32-16:09 (AEC enabled→disabled, trust-score tested→reverted).
- Existing AEC code: `core/aec_speex.py`, `core/aec_reference.py`, `core/aec_pipeline.py`, `core/aec_calibrate.py` — will be removed in Phase 4.
- Existing wake gate: `scripts/voice_listener.py:2060-2200` (music_mode, trust_score, energy_floor) — will be removed in Phase 4.
- ReSpeaker hardware notes: `~/.claude/projects/-Users-alexanderberardi-jarvis/memory/project_respeaker_hat.md`.
