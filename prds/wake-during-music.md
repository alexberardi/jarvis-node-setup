# Wake-during-music — fix Speex AEC (delay + filter length + PGA)

**Status:** draft, feat/wake-during-music. Phase 2 analysis complete
([analysis.md](wake-during-music/analysis.md)) — algorithm decision
revised from "stereo + multichannel AEC3" to "single-channel Speex
with corrected delay, longer filter, and tuned PGA".

## Problem

"Hey Jarvis" fails to fire when music plays through the same speaker the node hears. Today's prod kitchen journal (v0.1.100, AEC disabled) over ~30 min of Spotify playback: **18 `wake-suppressed-music-bleed` events vs 4 `Wake fired`** (2 of those via the 0.95 trust-score bypass). Reported in the wild as "Hey Jarvis just doesn't work while music is playing — I have to turn the volume down to talk to it."

Today's investigation ruled out three would-be fixes:

- **Speex AEC (commit 47ce7b7, `aec_enabled=False` default)** — enabled on dev and exercised against Spotify. Startup calibration consistently failed (`too few mic samples collected | collected=7680, target=105600` — got exactly one AudioBus chunk before queue-empty timeout). Falling back to the static 80 ms delay, Speex's adaptive NLMS filter could not converge against music transients: suppression bounced between -2 and +4 dB, with one momentary 7.8 dB spike that decayed within seconds. The net effect was wake scores no better than with AEC off, and occasionally worse.
- **Lower the `wake_music_trust_score` bypass from 0.95 → 0.75** — applied to dev and tested. At loud playback (baseline_rms 11k-13k) OWW scores capped at 0.14-0.43 because the model can barely see "Hey Jarvis" through the bleed. No trust-score value rescues a 0.4 score that needs to be 0.5. The change is theater for the loud-music regime.
- **Lower the mic capture PGA from 60 % (+35.5 dB, install.sh default) to 49 % (+29 dB) and 42 % (+25 dB)** — captured stereo recordings via `parec` and found the ADC was **digitally clipping** at the default gain (peak 0.00 dB, flat factor 13.9, bit-depth 16/16 with 634 saturated samples on the right channel in a 15-second window). Dropping the gain eliminated clipping and got a few legitimate wakes to fire — but exposed a new failure mode: with the mic gain low enough that music doesn't saturate the ADC, music itself starts to **pattern-match the wake phrase**. Multiple "wake fired" events at 0.95+ scores with `pre_wake_speech_seconds: 0.0` (no voice activity in the 5-second pre-wake window) — music-only false-wakes. Each false-wake triggers a CC roundtrip → returns `not_for_me` → escalates to a 60 s cooldown that blocks real user attempts. PGA is a tradeoff dial, not a fix: high values clip real voice, low values let music spoof the wake.

The bottleneck is the OWW input — not the gate, not the trust-score, not the gain. The model needs cleaner audio than a single bleed-saturated mic can provide, *and* a way to tell user-voice apart from music that pattern-matches the wake phrase.

## What the data says (Phase 2 result)

Cross-correlation of the music-only mic + PA-monitor recordings
([analysis.md](wake-during-music/analysis.md)):

- **Actual per-Pi acoustic delay: 64 ms** (Speex's hardcoded fallback
  was 80 ms; the original AEC commit guessed 100-240 ms).
- **Reverb tail to -30 dB: 149 ms** (Speex's filter is 100 ms — too
  short by 50 % to model this room).
- **Coherence(mic, ref) at 64 ms delay: γ² = 0.891 median**, implying
  a theoretical linear-AEC ceiling of **~9.6 dB** of music-bleed
  suppression — enough to take OWW from "can't see the phrase" to
  "can see it clearly".
- **Coherence(mic-L, mic-R): γ² = 0.85 median.** Second mic is mostly
  redundant at this geometry; the stereo refactor described in the
  original PRD draft would have been a big plumbing project for ~15 %
  new information.

The Speex algorithm class is fine. **What's broken is the calibration
(delay 16 ms off) and the filter length (100 ms vs 150 ms needed).**
Plus the mic PGA, which clips at install default and creates music
false-wakes when dropped too low.

## Success criteria

> At Spotify normal kitchen listening volume, "Hey Jarvis" fires within 1 wake-attempt cycle with **≥90% reliability**, with **no more than 1 music-only false wake per hour** of continuous playback.

Measured against a fixed test corpus:
- 20 wake utterances spoken at the dev Pi at 1.5 m from the node, recorded across 3 music genres (vocal pop, percussive electronic, ambient instrumental) at 2 volume levels (moderate ~baseline_rms 2-3k, loud ~baseline_rms 8-12k).
- 60 minutes of continuous music in each genre/volume without intentional wake attempts → false-positive count.

Off-ramp: if CPU on Pi Zero 2W can't sustain real-time stereo AEC3 + OWW at 16 kHz, fall back to ICA-based separation (lighter CPU, less convergent) or accept "moderate volume only" success.

## Phased plan

| Phase | Scope | Deliverable | Risk |
|---|---|---|---|
| **0** ✓ | This PRD | Agreed plan, success metric, off-ramps | — |
| **1** ✓ | Diagnostic recordings via `parec` against PA source on dev Pi. Voice-only, music-only, voice+music, all stereo s16le 48 kHz. Music+voice and music-only also captured PA monitor reference. | WAVs in `prds/wake-during-music/recordings/`; session notes in `findings-2026-06-03.md`. | — |
| **2** ✓ | Offline analysis (`analyze.py`) — cross-correlation delay, mic-L/R coherence, voice-vs-music PSD overlap, reverb tail decay, mic-vs-ref coherence ceiling. | [`wake-during-music/analysis.md`](wake-during-music/analysis.md). Decision: keep Speex, fix calibration + filter length + PGA. Drop the stereo refactor. | — |
| **3** | Fix Speex AEC. (a) `core/aec_calibrate.py`: replace the broken chirp-based calibration with natural-music cross-correlation over the first 5 s of detected playback. (b) `scripts/voice_listener.py`: defaults `aec_reference_delay_ms=64`, `aec_filter_length_ms=150`, `aec_enabled=true`. (c) CPU benchmark on Pi Zero 2W at 2400-tap filter; if it busts the real-time budget, drop to 2000 (125 ms) and document the tradeoff. | Speex actually working on dev with sustained ≥6 dB suppression during music. | CPU budget. Need to verify the AudioBus subscriber starvation that broke the old calibration doesn't also affect the new measurement path. |
| **4** | Wire it up. Retire `wake_music_trust_score` / `wake_word_music_energy_multiplier` / `wake_word_threshold_music` — the entire music-mode energy gate becomes redundant. `install.sh`: lower PGA default from 60 % to ~49 % (or whatever Phase 5 validation picks). | Single clean wake path; old music-mode gate code deleted. | Regression on non-music wake reliability — Phase 5 catches. |
| **5** | Validation against the success-criteria corpus. A/B vs `main` baseline at matching volumes / music genres. Run dev Pi for ≥1 week. | Ship/no-ship report. If ship: tag, GitHub pre-release, deploy to dev. Promote to prod after a week of clean dev runtime. | Real-world music range may exceed our test corpus. |

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
