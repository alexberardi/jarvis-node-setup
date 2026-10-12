# Acoustic test tool

Tests the whole voice path with a real wake word and real audio, without anyone present: Home Assistant speaks
`Hey Jarvis, <phrase>` through a smart speaker placed near the node, and the tool reads the node's log to see what
happened (wake score, transcript, route, reply, time to first audio). Because the audio is the same every run, wake
threshold, Whisper prompt, microphone gain or distance can be compared on identical phrases.

| File | Purpose |
|---|---|
| `run.py` | Speak a list of phrases and record one result line per phrase |
| `mic_level.py` | Measure the microphone level at given speaker volumes and the room noise floor |
| `log_parser.py` | Pure log parsing (unit-tested in `tests/test_acoustic_test_tool.py`) |

## Setup

You need a Home Assistant with a text-to-speech engine, a media player (a Cast speaker works; others should) near the
node, and a way to read `docker logs` for the node (ssh or local). Set the environment variables documented at the top of
`run.py` (`HA_URL`, `HA_TOKEN` or `HA_TOKEN_FILE`, `PLAYER_ENTITY`, `NODE_SSH`, and optionally `SERVER_SSH`).

```bash
export HA_URL=http://homeassistant.local:8123 HA_TOKEN_FILE=~/.ha-token
export PLAYER_ENTITY=media_player.kitchen_speaker NODE_SSH=pi@jarvis-node.local

echo '["what time is it", "set a timer for ten minutes", {"utt": "did you feed the dog", "wake": ""}]' \
  | python -m tools.acoustic_test.run --out runs.jsonl --volume 0.3
```

An item with `"wake": ""` is chatter that should **not** be answered; it is reported as `miss` (no wake) or `answered`
(a false answer). Verdicts per phrase are `answered`, `miss` (the wake model did not fire), `dropped` (woke, but the
request was judged not addressed to the node) or `error`.

## Reading the results

- **Volume first.** Run `python -m tools.acoustic_test.mic_level 0.15 0.35` and compare the speech peak with the noise
  floor's peak. Speech at or below the floor is not heard by the wake model, which is what a quiet voice across a room
  looks like. Pick the volume that matches a person at the distance you care about; a loud speaker beside the
  microphone flatters every result.
- **Repeat.** Identical audio can score 0.79 on one run and 0.00 on the next (speaker start-up clipping, household
  noise). Use a few dozen phrases, not three.
- **Stop the node** before recording a clip bank: it answers the clips and records its own replies.

## Limits

The log parser relies on the node's INFO log lines (`Wake fired`, `Recording complete`, `Transcription received`,
`Speaking ... via`); a change to those messages needs a change to `log_parser.py`. Server-side fields (route,
wake-verification verdict) are only filled when `SERVER_SSH` can read command-center's log.
