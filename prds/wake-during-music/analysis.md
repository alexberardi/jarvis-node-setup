# Phase 2 analysis — algorithm decision

Numbers from `analyze.py` against the 2026-06-03 recordings. Each finding
includes the implication for the Phase 3 algorithm choice.

## Q1: True acoustic delay (mic vs ref)

| | Value |
|---|---|
| Cross-correlation lag | **3083 samples @ 48 kHz = 64.2 ms** |
| Peak SNR (peak / median \|corr\|) | **50.8** (strong, unambiguous) |
| Peak sign | **Negative** (signal arrives mic-side phase-inverted; expected from this analog chain) |

**Implication:** Speex's hardcoded fallback was 80 ms. The actual per-Pi
delay is 64 ms. The 16 ms error is roughly half the tolerance window
(see Q5 sweep), which by itself reduces achievable suppression by
several dB. Calibration didn't fix it because the calibration code's
mic-capture loop starved on the running AudioBus subscriber and gave
up after one chunk.

## Q2: Mic L vs Mic R coherence (does stereo help?)

`music_only_mic.wav`:

| Band | Median γ²(L, R) |
|---|---|
| 80 – 500 Hz | 0.990 |
| 500 – 2000 Hz | 0.896 |
| 2000 – 4000 Hz | 0.770 |
| 4000 – 8000 Hz | 0.909 |
| **80 – 4000 Hz (wake band)** | **0.850** |

**Implication:** With the two mics 30 mm apart and the speaker hundreds
of millimeters away, both channels carry essentially the same signal
across the wake band. Adding a second mic to the algorithm input gives
~15 % new information — not zero, but not the main lever. **The stereo
refactor described in the original PRD is over-investment.** A
single-mic AEC with the reference channel does almost all the
achievable work.

## Q3: Voice vs music spectral overlap

| | Frequency / fraction |
|---|---|
| Voice spectral peak | 434 Hz |
| Music spectral peak | 598 Hz |
| Wake-band fraction where voice ≥ 3 dB louder than music | **24.2 %** |

**Implication:** Voice has spectral distinctness — fundamental + first
formant land below typical music energy — but ~76 % of the wake band
is dominated by music when both are present. Spectral subtraction
alone isn't going to do it; we need the AEC's time-domain cancellation
to pull music out, then OWW can use whatever voice-specific structure
survives in the cleaned signal.

## Q4: Reverb tail / impulse-response decay

Estimated from cross-correlation of `music_only_mic.wav` vs
`music_only_ref.wav`:

| | Value |
|---|---|
| Peak lag (direct-path arrival) | 64.2 ms |
| Decay to −30 dB below peak | **149.0 ms** |

**Implication:** Speex was configured with `filter_length = 1600`
taps at 16 kHz = 100 ms of impulse response. That's **shorter than
the actual room's −30 dB decay**. The adaptive filter literally
cannot represent the tail of the echo, so part of the music bleed is
mathematically unreachable. Phase 3 needs filter length ≥ 2400 taps
(150 ms) on the Pi Zero CPU budget, ideally 3200 taps (200 ms) for
headroom.

## Q5: Coherence(mic, ref) — theoretical AEC ceiling

`music_only_mic.wav` vs `music_only_ref.wav` with the measured 64.2 ms
delay applied:

| | Value |
|---|---|
| Median γ² (80–4000 Hz) | **0.891** |
| p10 γ² | 0.548 |
| p90 γ² | 0.974 |
| Implied max linear-AEC suppression | **9.6 dB (median band)** |

Delay sweep (sanity check that we're at the right alignment):

| Delay applied | Median γ² | Max suppression |
|---|---|---|
| −30 ms | 0.009 | 0.0 dB |
| 0 ms (raw, no alignment) | 0.010 | 0.0 dB |
| +30 ms | 0.097 | 0.4 dB |
| **+64 ms (measured)** | **0.891** | **9.6 dB** |
| +128 ms | 0.015 | 0.1 dB |
| +200 ms | 0.006 | 0.0 dB |

**Implication:** Linear AEC absolutely works on this hardware — the
ceiling is ~10 dB of music-bleed suppression, which is enough to take
OWW's score from 0.2-0.4 (where it can't fire) to 0.5+ (where the
standard threshold catches a real wake). But the delay has to be
right within ±30 ms or coherence collapses to near zero. Speex's
broken calibration putting us at 80 ms is right on the edge of useful
— some of today's intermittent successes were probably the filter
catching a fortunate moment of partial alignment.

## Recording-quality artifacts

- `voice_music_mic.wav` clipped on voice peaks (0.015 % L, 0.167 % R
  of samples at full-scale). Q2 coherence dropped to 0.71 median for
  this take vs 0.85 for `music_only_mic.wav`, mostly in the
  2000-4000 Hz band where voice formants live and the clipping was
  worst.
- The opposite-sign cross-correlation peak (Q1 `peak_corr =
  -2494.88`) is the analog chain's phase inversion. Doesn't affect
  AEC effectiveness — Speex / WebRTC adapt to phase — but worth
  noting if anyone tries to debug by ear.

## Algorithm decision

**Single-channel adaptive AEC, fed by the PA monitor source as
reference, with the following corrections to the current Speex
implementation:**

1. **Per-Pi delay calibration that actually works.** Either:
   - Fix the AudioBus subscriber starvation that's killing the chirp
     calibration in `core/aec_calibrate.py`, OR
   - Drop the chirp method entirely and use cross-correlation against
     the natural music reference for ~5 s after first detected
     playback to find the delay. (More robust because it uses the
     actual signal the user plays, not a synthetic chirp into a
     suspended sink.)
2. **Filter length ≥ 2400 taps** (150 ms at 16 kHz). Current 1600 is
   too short. Profile CPU on Pi Zero 2W with 2400 and 3200 to find
   the headroom limit.
3. **Residual echo suppressor on top of Speex.** Q5 says median γ² =
   0.89 but p10 = 0.55 — there are frequency bins where linear
   cancellation only gets 3 dB. A simple over-subtraction stage
   (Speex has one) or a Wiener post-filter would pick up another
   3-5 dB on hard-to-cancel bands.
4. **Mic PGA tuned for the new algorithm.** Today's 60 % clips during
   music; 42 % causes music false-wakes; 49 % is intermittent. With
   AEC actually pulling music out before OWW sees the signal, we can
   afford a lower PGA (better headroom) because the algorithm is
   doing the noise rejection rather than relying on the gain stage.
   Target probably 45-50 % once Phase 3 is live.

**Explicitly NOT pursuing:**

- **Stereo mic refactor.** γ²(L, R) = 0.85 — second channel is mostly
  redundant. The bus + ALSA + downstream-consumer plumbing churn
  isn't justified by 15 % new info.
- **ICA / blind source separation.** Requires statistical independence
  between channels; we have the opposite. Wrong tool.
- **WebRTC AEC3.** Worth keeping in our back pocket as a fallback if
  the Speex fixes don't deliver 8+ dB sustained, but it's a larger
  binding-and-integration project and the data says we don't need it.

## Revised Phase 3 scope

Was: "stereo capture refactor + multichannel algorithm" — would have
been a multi-week project.

Now: **fix the Speex AEC** — delay calibration + filter length +
PGA tune. Estimated scope: a few days of node-setup work plus a few
days of validation on the dev Pi.

Concrete patch list for Phase 3:

| File | Change |
|---|---|
| `core/aec_calibrate.py` | Rewrite to use natural-music cross-correlation OR fix the AudioBus chirp starvation (instrument `q.get` timings first to see which is cheaper). |
| `scripts/voice_listener.py` | Default `aec_filter_length_ms` to 150 (currently 100). |
| `scripts/voice_listener.py` | Default `aec_reference_delay_ms` to 64 (currently 80). Calibration overrides this on first run. |
| `scripts/voice_listener.py` | `aec_enabled` default → `true` (currently `false`). |
| `install.sh:751` | Lower PGA from `60 %` to `49 %` (or whatever final number Phase 5 validation picks). |
| (delete) | `wake_music_trust_score` / `wake_word_music_energy_multiplier` / `wake_word_threshold_music` — the entire music-mode energy gate logic becomes redundant once AEC actually works. Defer until Phase 4 once we've proven AEC carries the load. |

## Open items

- The 9.6 dB theoretical ceiling is a **median**. In music sections
  dominated by drums + bass (high coherence) we'll get more; in
  vocal-heavy or high-frequency-percussion-heavy sections we'll get
  less. Phase 5 validation needs at least three music genres to
  exercise the range.
- Calibration via natural music requires a few seconds of playback
  before we have a delay estimate. Bootstrap question: what delay do
  we use *until* the first calibration completes? Probably the 64 ms
  measured here as the new default, calibration then refines per-Pi.
- CPU budget on Pi Zero 2W with a 2400-tap Speex filter is unknown.
  Worst case we accept a 12-15 % CPU hit on the wake loop.
