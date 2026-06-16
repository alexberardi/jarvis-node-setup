# Running a voice node in a container (with audio)

Run the full voice runtime — mic capture, wake word, speaker — in Docker on a
Linux host, with the host's audio passed in. This is the container counterpart
to the native Pi install.

> **macOS doesn't work.** Docker Desktop runs containers in a VM with no access
> to the Mac's audio hardware. You need a **Linux host with audio**: an Ubuntu
> server/desktop or a Raspberry Pi running Linux + Docker, with a mic + speaker
> (a USB audio device is simplest).

The image is `Dockerfile.audio` (separate from the lightweight headless
`Dockerfile`). It adds the audio stack (PortAudio, Speex, ALSA/PulseAudio
client tools), installs the audio + wake-word Python deps, and pre-bakes the
openWakeWord models.

---

## Step 1 — Quick start (ALSA `/dev/snd`)

This path passes the host's sound devices straight through with `/dev/snd`. It
works whether or not the host runs PulseAudio, so it's the best first test.

### 1. Build the image

Building needs the `jarvis-command-sdk` sibling repo checked out next to this
one (`../jarvis-command-sdk`).

```bash
docker compose -f docker-compose.audio.yaml build
```

This builds for the host's architecture (arm64 on a Pi, amd64 on an Ubuntu
box). First build pulls onnxruntime/scipy/numpy and the wake models, so give
it a few minutes.

### 2. (Optional) pick your audio devices

If the host has exactly one audio device, skip this — playback defaults to ALSA
`default` and the mic auto-selects the first input. With multiple devices:

```bash
./scripts/configure-audio.sh
```

It lists the mics/speakers **as the container sees them** and writes
`audio.env` (git-ignored), which compose loads automatically.

### 3. First run → register the node

```bash
docker compose -f docker-compose.audio.yaml up
```

On first boot there are no credentials, so the node starts the **setup web UI**
on `http://<host>:7771`. Open it, log in, pick a household/room, and it
registers with the command center (point it at your CC URL there). Credentials
are written to the `jarvis-node-config` volume.

### 4. Restart into voice mode

```bash
docker compose -f docker-compose.audio.yaml restart
```

Now it has credentials and `JARVIS_NODE_MODE=voice`, so it starts the full
voice runtime: it listens for the wake word, records, sends to the command
center, and plays the response back through the host speaker.

---

## Configuration

Set these in `audio.env` (or `configure-audio.sh` writes them). All are
optional — sensible defaults apply.

| Variable | Meaning | Default |
|---|---|---|
| `JARVIS_AUDIO_OUTPUT_DEVICE` | ALSA device for `aplay -D` (e.g. `plughw:1,0`, a named sink, or `default`) | `default` |
| `JARVIS_MIC_DEVICE_INDEX` | PyAudio input index (see `configure-audio.sh`) | auto (first input) |
| `JARVIS_MIC_DEVICE_NAME` | substring match against an input device name (more stable than index) | — |
| `JARVIS_MIC_SAMPLE_RATE` | mic capture rate; the runtime resamples to 16 kHz. Try `44100` for USB mics that reject 48 kHz | `48000` |

The node never runs the ReSpeaker/TLV320 driver workarounds in a container
(`JARVIS_NODE_OS=LINUX` is baked into the image), even if a HAT is visible via
`/dev/snd`.

---

## Troubleshooting

- **No input devices listed / `PyAudio.open()` fails** — the mic isn't reaching
  the container. Confirm `/dev/snd` exists on the host and `arecord -l` shows
  the device; check the container user is in the `audio` group (compose sets
  `group_add: audio`).
- **Playback silent / `aplay` errors** — set `JARVIS_AUDIO_OUTPUT_DEVICE` to a
  real device from `aplay -L` (run `aplay -L` on the host or inside the
  container). `default` may point at the wrong card on multi-device hosts.
- **Wake word never fires** — check the mic with
  `docker compose -f docker-compose.audio.yaml exec jarvis-node-audio \
  python scripts/list_audio_devices.py`, and lower `JARVIS_MIC_SAMPLE_RATE` if
  the device doesn't support 48 kHz.
- **Can't reach the command center** — on Linux the container reaches host
  services via `host.docker.internal` (mapped in the compose). Use that host in
  the setup UI if CC runs on the same machine.

---

## Step 2 — Shared host PulseAudio (full control surface) — *not yet wired*

`/dev/snd` gives capture + wake + playback, but the runtime's volume control,
music ducking, and Bluetooth speak `pactl`/`parec` against a PulseAudio server.
To keep those working, the container shares the host's PulseAudio socket — which
requires running the container as your host user (PulseAudio rejects root). That
wiring (non-root uid + socket/cookie mounts) lands next; see
`prds/cross-platform-node.md` Phase 4.
