# `not_for_me` Detection & Loop Suppression — Improvement Plan

## Problem

Jarvis gets caught in loops, fires on ambient sound, and triggers on side
conversations that aren't directed at it. The pain is most acute when a
side conversation is happening near the node: a single wake fires →
`<not_for_me/>` (good) → but the same conversation re-fires wake on the
next sentence, and so on.

We want to keep general/casual chats working ("Jarvis, what's the
weather"), but get aggressive about rejecting overheard speech once
acoustic evidence points that way.

## Current state (snapshot)

What's already in the code (so we don't redo work):

- **Direction hint** — pre-wake VAD (~5s window). `<0.5s` speech → "quiet,
  directed signal". `>4.5s` → "ambient/overheard". 0.5–4.5s sends no hint.
  `app/core/direction_hint.py:32-60` (command-center).
- **`<not_for_me/>` LLM sentinel** with 5 cues + a universal "borderline →
  answer" bias. `app/core/prompt_providers/shared/core_rules.py:73-100`.
- **Recording-duration heuristic** rejects when speaker never paused AND
  transcript >20 words. `scripts/voice_listener.py:1054-1058`.
- **Follow-up window** opens after every reply: 4s default (was 15s),
  decays, max 5 iterations. Exits on `not_for_me`, self-echo ≥85% overlap,
  or 2 consecutive lone-word turns. `scripts/voice_listener.py:1232+`.
- **Wake-debounce constants** (`_WAKE_DEBOUNCE_SEC = 8.0`,
  `_last_wake_ts`) **declared but never read**. `scripts/voice_listener.py:556-558`.
- **Barge-in** — two-tier OWW + energy gate during TTS. `core/barge_in.py`.

## Shipping now (this PR set)

**#1 — Hard quiet-period after `<not_for_me/>`.** When the server signals
`not_for_me`, suppress wake on the node for ~20s (configurable). Side
conversations cluster in time; this directly kills the loop class where
the next sentence of the same conversation re-triggers wake.
Tradeoff: a real follow-up immediately after a misclassification gets
silenced for the cooldown window.

**#2 — Wire up the existing wake debounce.** `_WAKE_DEBOUNCE_SEC = 8.0`
exists but isn't enforced. Merging with #1 into one gate
(`_wake_min_next_ts`) means a single check at the wake-fire site covers
both same-utterance double-fires and post-`not_for_me` cooldown.

**#4 — Flip the LLM borderline bias when ambient hint is present.**
Current prompt says "Silencing a real request is worse than answering an
overheard one" unconditionally. Make that bias conditional on the
direction hint: keep it for `quiet`/no-hint, flip it to "require explicit
addressing or imperative shape, otherwise emit `<not_for_me/>`" when the
hint says ambient/overheard. The node has already done the acoustic work;
the prompt should stop overriding it on ambiguous transcripts.

## Follow-up after testing (not in this PR)

Ordered roughly by expected payoff / cost ratio. Re-evaluate after #1+#2+#4
land and we have a couple weeks of real-world data.

**#3 — "No-pause-throughout" check using Whisper word timestamps.**
Today we only reject when `max_duration + >20 words` both hit. Use
Whisper's per-word timestamps to compute the maximum inter-word gap. If
the recording has no gap >250–400ms AND >8 words, that's narration shape,
not command shape. Catches mid-sentence wakes earlier than the duration
ceiling. The user proposed this directly — likely the next highest-value
move after #1/#2/#4.

**#5 — Enrich the direction hint with more dimensions.** Today it's a
binary quiet/ambient/silent. Add:
- *Time since last Jarvis utterance* — wake fired 1.2s after I stopped
  speaking is a very different signal than a cold-start wake.
- *Speech onset vs. mid-utterance* — did mic energy ramp up after wake
  or was it already saturated?
- *Speaker count in the captured audio* — Whisper already returns
  speaker info; ≥2 distinct speakers = almost certainly side conversation.

**#6 — Two-speaker gate in the follow-up window.** Single voice
continuing = real follow-up. Two voices appearing during the open mic =
close the window. Uses existing speaker-recognition data.

**#7 — Require positive evidence in the LLM check.** Current 5 cues are
all *negative*. Add a required positive: imperative verb / question to
assistant / named address. Borderline + no positive evidence →
`<not_for_me/>`. Pairs naturally with #4 — that change moves the prompt
in this direction; #7 makes it explicit.

**#8 — Voice-match the follow-up window.** Fingerprint the wake speaker's
timbre; require timbre match in the follow-up window. Stronger version of
#6, but real implementation cost (voice-print model on the Pi).

## Explicitly *not* doing

- **Tightening the global OWW wake threshold.** Trades false wakes for
  missed real wakes. The user has historically wanted Jarvis to wake
  reliably; the answer is better post-wake filtering, not a higher floor.

## Implementation notes

### #1 + #2 — shared gate (`scripts/voice_listener.py`)

Replace the unused `_last_wake_ts` + `_WAKE_DEBOUNCE_SEC` constants with
a single `_wake_min_next_ts` and `suppress_wake_for(seconds, reason)`
helper:

- Wake-fire site (~line 1968): if `time.monotonic() < _wake_min_next_ts`,
  set `fire_wake = False` and log. On accepted wake, push the gate to
  `now + _WAKE_DEBOUNCE_SEC`.
- After `send_for_transcription` returns (~line 2189), branch on
  `result.get("not_for_me")`. If true, call
  `suppress_wake_for(Config.get_float("not_for_me_quiet_seconds", 20.0),
  reason="not_for_me")`. Else, the existing `_record_legitimate_wake_score`
  path runs as today.

Multiple callers don't stack — the later/longer suppression wins.

### #4 — prompt change (`app/core/prompt_providers/shared/core_rules.py`)

Rewrite the borderline-cases paragraph at the bottom of
`NOT_FOR_ME_INSTRUCTION` to branch on the `[direction hint:]` line.
Preserve every phrase referenced by `tests/test_core_rules.py`
("Silencing a real request is worse", "thanks", "never mind",
"Mid-narrative fragment", etc.) — move them under the appropriate
branch rather than removing.

## Open questions / things to watch in testing

- Is 20s the right `not_for_me_quiet_seconds` default? Too short = loop
  reappears; too long = real follow-ups get dropped. Start at 20, watch
  logs for `wake-suppressed` fires that look like real requests.
- Does flipping the borderline bias under ambient hint cause an uptick
  in `<not_for_me/>` on real requests where the room happened to be
  noisy? The 4.5s threshold for "ambient" is conservative; if false
  silences increase, raise it to 5.5s rather than reverting the bias.
- Does the debounce gate ever fight the follow-up loop? The follow-up
  loop uses its own listen path (no wake-word required), so the wake
  gate shouldn't affect it — but worth confirming on the node.
