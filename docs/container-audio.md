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

## Easiest: let the setup script decide

After building the image (`docker compose -f docker-compose.audio.yaml build`),
run:

```bash
./scripts/configure-audio.sh
```

It detects your host's audio transport (PipeWire/PulseAudio socket vs raw
`/dev/snd`), lets you pick the mic, writes `audio.env`, and prints the exact
`docker compose … up` command for your host (including the pulse overlay +
`JARVIS_HOST_UID` when needed). On macOS it tells you to run natively instead.

The two sections below are the manual reference for each transport.

## Step 1 — Quick start (ALSA `/dev/snd`)

This path passes the host's sound devices straight through with `/dev/snd`.
**Use it only on a host with no sound server** (e.g. a headless server with a
USB mic/speaker). On a Linux **desktop** the running PulseAudio/PipeWire owns
the devices, so raw `/dev/snd` is exclusive — capture may work but playback
fails with "Device or resource busy". There, use **Step 2** (the pulse
overlay) instead.

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

## Step 2 — Shared host PulseAudio / PipeWire (recommended on a desktop)

On a host that runs a sound server (any Linux desktop with PipeWire, or
PulseAudio), route audio through its socket instead of `/dev/snd`. This is
**required** there — the sound server owns the devices, so `/dev/snd` playback
is busy — and it's the better path generally (the server mixes, handles
full-duplex, and keeps `pactl` volume/ducking working).

Add the pulse overlay on top of the base file:

```bash
docker compose -f docker-compose.audio.yaml -f docker-compose.pulse.yaml up
```

It bind-mounts the host PulseAudio socket (`/run/user/<uid>/pulse/native`),
sets `PULSE_SERVER`, and forces playback to the `pulse` device (→ your host
default sink). If your login uid isn't 1000, pass it:

```bash
JARVIS_HOST_UID=$(id -u) docker compose -f docker-compose.audio.yaml -f docker-compose.pulse.yaml up
```

Notes:
- **No non-root rework needed** with PipeWire's pipewire-pulse — it accepts the
  container as root. (A classic PulseAudio daemon configured to reject root
  would need the container to run as your uid; pipewire-pulse, the Linux
  desktop default, does not.)
- Playback follows your host **default sink** — set that to the right output in
  your desktop sound settings (`pactl set-default-sink ...`).
- Capture still uses the mic from `audio.env` / auto-select over `/dev/snd`
  (the base file passes it through). To also capture via the sound server, set
  `JARVIS_MIC_DEVICE_NAME=pulse` in `audio.env`.
- Verified working: containerized node on an Ubuntu/PipeWire desktop —
  "Hey Jarvis" → wake → STT → command-center → spoken response out the headset.
