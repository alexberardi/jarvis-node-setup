# Cross-platform node — full 64-bit Pi + container with full audio

**Status:** draft, 2026-06-16. Branch: `feat/cross-platform-node` (worktree).
Phase 0 landed. Author lean captured at bottom.
Companion to `audio-playback-architecture.md` / `wake-audio-split.md` (those are
the *Pi-HAT audio internals*; this is *where the runtime can run at all*).

## Problem

`jarvis-node-setup`'s full audio runtime (mic capture → wake → STT/voice →
playback) is effectively single-target today: **Pi Zero 2 W + ReSpeaker 2-Mic
HAT v2 (TLV320AIC3104)**. We want two more targets:

- **Goal A — any full 64-bit Raspberry Pi (aarch64).** Pi 3 / 4 / CM4 / Pi 5.
- **Goal B — the container image, with the *same* audio support** (mic + wake +
  playback) running on a Linux host, configured by an interactive
  device-selection setup script that writes the host's chosen mic/speaker into
  the compose env.

The container is the hard half: today it deliberately ships **zero audio** —
`Dockerfile:13` installs `requirements-base.txt` (a list explicitly titled "no
audio/wake-word libs"), and the entrypoint runs `text_mode.py`, which
"skips all audio I/O, wake word detection, and Bluetooth"
(`scripts/text_mode.py:4-5`).

## Decisions (locked — 2026-06-16)

1. **Standardize on 64-bit (aarch64).** Drop the armv7l path. The release CI
   *already* builds arm64 only and the release notes already list "Pi Zero 2W /
   Pi 3 / Pi 4 / Pi 5 (arm64)" (`.github/workflows/release.yml:24-28,121`), so
   the 32-bit branches in `install.sh` are vestigial. 64-bit is the reliability
   win: `onnxruntime` has **no armv7l wheel at all** (today's wake-word
   fragility) but ships clean aarch64 wheels.
2. **Pi 5 (bcm2712) is in scope.** Needs device-tree work (see Goal A #3).
3. **Container audio transport = share the host PulseAudio socket**, not
   `--device /dev/snd`. The runtime's whole control surface speaks
   `pactl`/`parec`/`paplay`; ALSA-direct kills volume/ducking/AEC/A2DP.
4. **AEC in the container is deferred** — ship wake + capture + playback first;
   it degrades gracefully (the reference reader goes inert,
   `core/aec_reference.py:129-141`). Real AEC later, ideally via host-side
   `module-echo-cancel`.
5. **Container hosts supported:** Linux server + USB audio; Pi 4/5 + ReSpeaker
   HAT. **macOS Docker Desktop is a hard ❌** for *containerized* audio (its VM
   has no host-audio passthrough) — the setup script detects macOS and steers
   to the native run path (`run-mac.sh`).

## Current state (snapshot) — what already exists (don't redo it)

- **Container image is published** (`ghcr.io/alexberardi/jarvis-node`,
  `release.yml:174-185`) — but **single-arch amd64** (the `build-push-action`
  has no `platforms:`). No arm64 image today.
- **Mic device is already configurable**: `utils/mic_device.py:52-53` reads
  `mic_device_name` / `mic_device_index` from `Config`, with a "first input
  device" fallback (`:97`). **Playback is not** — see the coupling list.
- **Entrypoint already mode-branches**: `scripts/entrypoint.py:50-57` →
  credentials present → `text_mode`; absent → `setup_mode`. There is **no voice
  branch**. Adding one is a clean insertion.
- **`scripts/setup_mode.py` already exists** but is an *in-container web wizard
  for node registration* (login → household → register). It is **not** audio
  config — name the new audio script distinctly to avoid confusion.
- **A single reliable Pi detector exists** (`provisioning/api.py:66`, reads
  `/proc/device-tree/model`) but is **not shared** — both
  `core/platform_abstraction.py:1380` and `services/led_service.py:62` use the
  flawed `platform.system()=="Linux"` → "it's a Pi" assumption.
- **`requirements-audio.txt` already exists** and is ~90% the container set —
  but it bundles HAT-only libs (`spidev`, `apa102-pi`, `gpiozero`) that can't
  install off-Pi (need `python3-lgpio` via `--system-site-packages`).
- **Compose already anticipates the non-root rework**: `docker-compose.yaml`
  carries a NOTE that the `/root/.jarvis/packages` mount is coupled to the
  Dockerfile having no `USER` (HOME=/root), "update this mount target" if that
  changes.
- **Dead code:** `core/platform_abstraction_enhanced.py` (28 KB) re-implements
  the whole provider hierarchy + the same flawed `get_platform()` and is
  **imported by nothing** — a trap, delete it.

## What's load-bearing today (do not regress)

- The HAT path on a real Pi node: ALSA `output` alias / `seeed2micvoicec`
  dsnoop, TLV320 mixer baseline + SUSPENDED-sink self-heal + keepalive
  (`utils/audio_volume.py`, `scripts/main.py`). Cross-platform work must leave
  the HAT path **byte-identical** when a HAT is present — gate new behavior, do
  not replace.
- The graceful-degrade chain in `scripts/main.py` (voice_listener retries then
  falls to headless MQTT) — a misconfigured audio container must not crash-loop.
- `mic_device_name`/`mic_device_index` config contract (`utils/mic_device.py`).

## Goal A — full 64-bit Pi (aarch64)

**Bottom line: with the same HAT, the audio runtime needs zero code changes on
a 64-bit Pi.** There is no 32/64-bit branching in the audio selection logic;
the overlay, asound, mixer, GPIO/SPI, provisioning, and BlueZ class are OS/HAT
couplings, not arch couplings. `install.sh` already maps `aarch64 → arm64`.

Gaps and fixes:

1. **CI: confirm the arm64 tarball is built+published with a matched
   interpreter.** The baked venv must use the **same Python minor** the target
   Pi OS ships (Bookworm 3.11 vs Trixie 3.13), or `rebuild_venv()`'s fast path
   misses and the Pi does a full on-device compile of numpy/scipy/onnxruntime.
   **Decide the target Pi OS** (recommend Trixie/3.13 to match the build image)
   and align `.python-version`/`detect_python`. **S.**
2. **Wheels get better.** aarch64 has official wheels for onnxruntime / numpy /
   scipy / scikit-learn. Wake-word becomes a normal pip install. Delete the
   `ARCH=armv7l` piwheels + onnxruntime-strip branches once 32-bit is dropped.
   **S.**
3. **Pi 5 (bcm2712) device-tree gap.** `setup/respeaker-2mic-v2_0-overlay.dts:12`
   lists `bcm2835/2708/2709/2711` — **not bcm2712**. So a Pi 5 gets a tarball
   (release notes already advertise it, `release.yml:121`) but the HAT won't
   bind. Needs a 2712 `compatible` + likely a Pi-5 I2S/clock overlay variant +
   hardware test. **M.** *(This is the one real Goal-A code/kernel task.)*
4. **64-bit Pi *without* the HAT** (USB mic / 3.5mm) needs the same playback-
   device decoupling as the container (Goal B) — shared work, not separate.

AEC works unchanged on 64-bit *with the same HAT* (`libspeexdsp1` ships arm64;
the known calibration flakiness is a TLV320/PA-state problem, not arch). **Goal
A effort: S–M** (S for HAT case; +M for Pi 5 overlay).

## Goal B — container with full audio

### B1. Audio transport — share the host PulseAudio socket

Pass the host's PulseAudio unix socket + cookie into the container; run the
container as the host user's uid; keep `pactl`/`parec`/`paplay` working. `/dev/snd`
ALSA-direct is the fallback only for a truly headless host with no sound server
(and then we must ship a configurable `output` device — `aplay -D output` will
fail without the install.sh-written `/etc/asound.conf`).

Hard requirements for the socket path:
- **Container runs as a non-root host uid** (Pulse rejects root /
  XDG_RUNTIME_DIR ownership). Today the image is root with `HOME=/root` mounts
  (`docker-compose.yaml`); relocate `/root/.jarvis/packages` and friends. The
  compose NOTE already flags this.
- **Real PulseAudio (not PipeWire-pulse) if Bluetooth A2DP-in is wanted**
  (project CLAUDE.md invariant #13). Capture/playback/AEC-monitor are fine
  under PipeWire-pulse.
- **Install `libpulse0` / `pulseaudio-utils` / `libasound2-plugins`** in the
  image so the CLI tools + ALSA→pulse bridge exist.

### B2. The host-side device-selection setup script (the UX)

A script run **on the host** (not in the container) that:
1. Detects transport (PulseAudio socket present? else ALSA).
2. Enumerates devices the way the container will see them —
   `pactl list short sources` (mics; filter out `.monitor`) and
   `pactl list short sinks` (speakers); ALSA fallback `arecord -l`/`aplay -l` →
   `hw:X,Y`. Auto-detect + pre-select the ReSpeaker card on a Pi.
3. Prompts for mic + speaker.
4. Writes an `audio.env` consumed by a new `docker-compose.audio.yaml`:
   `JARVIS_AUDIO_TRANSPORT`, `JARVIS_MIC_DEVICE`, `JARVIS_OUTPUT_DEVICE`,
   `JARVIS_VOICE_MODE=voice`, plus `PULSE_SERVER`/`PULSE_COOKIE`/
   `XDG_RUNTIME_DIR` and host `UID:GID`.
5. On macOS: detect Docker Desktop, explain containerized audio isn't possible,
   steer to native (`run-mac.sh`).

Name it distinctly from the existing `setup_mode.py` (suggest
`scripts/configure-audio.sh` or `tools/select-audio-devices.sh`). The runtime
side reads `JARVIS_MIC_DEVICE`/`JARVIS_OUTPUT_DEVICE` in the new audio provider —
i.e. this script is the host-side half of the Phase-2 "make playback
configurable" code change.

### B3. AEC posture — deferred

Reference is 100% `parec` on a PulseAudio `.monitor` source
(`core/aec_reference.py:41,128-141`), already fragile on hardware. A `.monitor`
*is* reachable over a shared socket in principle but **unverified** — must test
`pactl list sources | grep monitor` inside the target container before relying.
Ship without AEC; revisit via host `module-echo-cancel` (Phase 6). A worthwhile
parallel refactor: abstract reference acquisition behind an interface (PA-monitor
/ ALSA-loopback / in-process tap) so it isn't 100% `parec`-on-monitor — helps
the HAT-less Pi too.

### B4. Capability matrix

| Host | Mic | Playback | Wake | AEC |
|---|---|---|---|---|
| Linux server + USB (shared host Pulse) | ✅ once libs+socket present | ✅ via configurable device | ✅ amd64/arm64 wheels | ⚠️ only if speaker+mic acoustically coupled; best via host `module-echo-cancel` |
| Pi 4/5 + ReSpeaker HAT (overlay on host, share socket / `/dev/snd`) | ✅ | ✅ HAT sink | ✅ arm64 wheels | ⚠️ same PA-monitor fragility as bare metal; Pi 5 needs overlay first |
| macOS Docker Desktop | ❌ no host-audio passthrough | ❌ | ❌ | ❌ |

### B5. Setup-script & container-audio edge cases (the operational shortcomings)

These are the assumptions the "share host Pulse + pick a device" design hides.
Each needs a real answer before Phase 4 ships.

1. **A headless Linux server often has NO running PulseAudio.** "Share the host
   socket" assumes a user-session Pulse exists; servers/appliances usually have
   none. The setup script must detect this and offer: (a) **ALSA-direct
   `/dev/snd`** (simplest for an appliance box — accept that volume/ducking/AEC
   degrade), or (b) **start a system-mode PulseAudio** (`pulseaudio --system`,
   discouraged-but-valid for an appliance). **Recommendation: the script probes
   `pactl info`; if no server, default to the ALSA-direct profile** and say so.
   This means the container provider must support *both* transports, not just
   Pulse — fold into the Phase-3 provider design.
2. **Pulse socket/cookie/uid resolution is host-specific.** User session →
   `/run/user/<uid>/pulse/native` + cookie `~/.config/pulse/cookie`; system mode
   → `/var/run/pulse/native`, no cookie. On a **Pi node the Pulse is a *user*
   service for user `pi`** (install.sh enables `pulseaudio.socket` per-user), so
   a container on that Pi must run as `pi`'s uid and mount that runtime dir. The
   script must resolve `XDG_RUNTIME_DIR`/uid/cookie, not hardcode them.
3. **Device names aren't stable across replug/reboot.** PulseAudio
   `alsa_output.usb-...` names and ALSA `hw:X,Y` indices can shift. Prefer
   by-id/stable identifiers where available; otherwise document "re-run
   `configure-audio.sh` if you move the USB device." Worth a stability note in
   the picker output.
4. **openWakeWord downloads models on first run** — in an ephemeral container
   that re-downloads every rebuild. **Bake the models into the image** (or mount
   a `jarvis-node-models` volume). Add to Phase 3.
5. **`/dev/snd` (ALSA-direct) needs the host's `audio` gid**, which varies. The
   script must resolve `getent group audio` → `group_add` in the override.
6. **Bluetooth audio is OUT of scope for "same audio support" in v1.** The
   runtime's BT features (A2DP phone-in, BT speaker out) need the host **system
   D-Bus socket + BlueZ access** passed into the container — a separate,
   harder passthrough than PCM audio. "Container with full audio" here means
   **mic + wake + speaker over the chosen device**, *not* Bluetooth. Call this
   out explicitly so it isn't assumed. BT-in-container is a future PRD.
7. **macOS is native-only.** The script steers Mac users to `run-mac.sh`; the
   container audio goal is simply not met on macOS (Docker Desktop has no audio
   passthrough). A `PULSE_SERVER=tcp:host.docker.internal:4713` shim is the only
   container option and is dev-curiosity only (latency, no AEC, manual host
   Pulse TCP module).

## Known shortcomings / gaps to resolve (from code verification)

1. **`requirements-audio.txt` bundles HAT-only libs** — can't install in a
   generic container. Split HAT libs (`spidev`/`apa102-pi`/`gpiozero`) into a
   `requirements-hat.txt`; container installs `base + audio` only.
2. **Published image is amd64-only** — add `platforms: linux/amd64,linux/arm64`
   to the `build-push-action` (`release.yml:175`) for Pi container hosts.
3. **Pi 5 advertised but HAT-unsupported** — release notes vs overlay mismatch
   (`release.yml:121` vs overlay `:12`). Either ship the bcm2712 overlay
   (Phase 5) or stop advertising Pi 5 HAT support until then.
4. **No shared Pi detector** — extract `is_raspberry_pi()` from
   `provisioning/api.py:66` and use it in `platform_abstraction.py` *and*
   `led_service.py` (both currently use `platform.system()`).
5. **Base `play_pcm_stream` hardcodes `aplay`** (`core/platform_abstraction.py:188`,
   inherited by *every* provider incl. MacOS) — the new container/linux-host
   provider must override it; MacOS arguably should too.
6. **`PiAudioProvider.play_audio_file` hardcodes `aplay -D output`**
   (`:534,:541`) — make the device alias configurable (default `default`/`pulse`
   when `output` absent).
7. **No container voice entry path** — add a `JARVIS_VOICE_MODE=voice` branch in
   `entrypoint.py` that runs `main.py` when credentials are present.
8. **`install.sh` rejects x86_64** (`:114-120`) — only matters for a *native*
   (non-container) amd64 Linux node; out of scope (container uses the Dockerfile
   path) but noted.
9. **AEC `.monitor`-over-socket = unverified** (see B3).
10. **Baked-venv Python vs target Pi OS** mismatch → slow on-device rebuild
    (Goal A #1).

## Phased plan

| Phase | Scope | Touches | Size |
|---|---|---|---|
| **0 — Cleanup + single Pi detector** ✅ | **Done.** Deleted dead `platform_abstraction_enhanced.py`; added cached `is_raspberry_pi()` (device-tree probe) in `platform_abstraction.py`; `get_platform()` now returns `MACOS`/`PI`/**`LINUX`** (non-Pi Linux is its own value), with a behavior-preserving `LINUX` seam in `create_audio_provider` (falls back to `PiAudioProvider` until Phase 3). **Left as-is on purpose:** `led_service._detect_pi()` is already capability-based (checks ACT-LED sysfs paths — more precise than a Pi check); `provisioning/api.py` keeps its granular `_get_hardware_type` (could unify onto the helper later). | `core/platform_abstraction.py` | S |
| **1 — Lock in 64-bit Pi (Goal A)** | Verify/exercise arm64 tarball in CI; pick target Pi OS + matched baked-venv Python; delete armv7l/piwheels/onnxruntime-strip branches. | `release.yml`, `install.sh`, `build/build-tarball.sh`, `.python-version` | S–M |
| **2 — Decouple audio devices from the HAT** ✅ | **Done** (3 commits). 2a: `get_output_device()` — `audio_output_device` setting, else `output` on the HAT / `default` elsewhere (`platform_abstraction.py`). 2b: cached `has_respeaker_hat()` gates the SUSPENDED-sink reload, `force_reload_alsa_card`, and the `main.py` keepalive (`audio_volume.py`, `main.py`). 2c: `MIC_RATE` from `mic_sample_rate` + rational-ratio resample so non-48k mics work (`voice_listener.py`, `wake_loop.py`). **LED not gated** — `led_service._detect_pi()` already keys on ACT-LED sysfs presence (capability-based). Capture device was already configurable (`mic_device.py`). Real-HAT behavior byte-identical; verified 163 tests pass (1 pre-existing failure on `main`: stale `Line=4` baseline assertion vs shipped `Line=2`). | `core/platform_abstraction.py`, `utils/audio_volume.py`, `scripts/main.py`, `scripts/voice_listener.py`, `core/wake_loop.py` | M |
| **3 — Container audio image (no AEC)** 🟡 | **Image done** (39c2a1e / d597283), built + validated on linux/arm64 — all audio deps + native libs (portaudio, libspeexdsp) import in-container: `Dockerfile.audio`, `requirements-hat.txt` split, `LinuxHostAudioProvider`, openWakeWord models pre-baked. Entrypoint switch is **`JARVIS_NODE_MODE=voice`** (not `JARVIS_VOICE_MODE` — would collide with the config `voice_mode` CC-response-style key). `play_pcm_stream` already uses `get_output_device()` (Phase 2a), so no override needed yet. **Remaining:** multi-arch CI publish (`platforms: linux/amd64,linux/arm64` + build the audio image in `release.yml`) so hosts can `docker pull` instead of building locally. | `Dockerfile.audio`, `requirements-*.txt`, `scripts/entrypoint.py`, `core/platform_abstraction.py`, `utils/audio_volume.py`, `release.yml` | M–L |
| **4 — Setup script + compose wiring** ✅ | **Done & validated end-to-end on an amd64 Ubuntu/PipeWire desktop.** Step 1 (91e30da): `docker-compose.audio.yaml` (`/dev/snd`), `scripts/configure-audio.sh`, `docs/container-audio.md` — for hosts with **no** sound server. Step 2: `docker-compose.pulse.yaml` overlay routes audio through the host PulseAudio/PipeWire **socket** (`-f docker-compose.audio.yaml -f docker-compose.pulse.yaml`; `JARVIS_HOST_UID` for non-1000). **Key correction to the original plan: pipewire-pulse accepts the container as ROOT — the non-root/uid rework is NOT needed on a modern desktop.** On a desktop, `/dev/snd` playback is busy (server owns devices) → the pulse overlay is required, not optional. Live test passed: wake → STT → CC → spoken TTS out the headset. **Remaining:** macOS→native steering (minor); fold pulse auto-detect into configure-audio.sh (nice-to-have). | `docker-compose.audio.yaml`, `docker-compose.pulse.yaml`, `scripts/configure-audio.sh`, `docs/container-audio.md` | M |
| **5 — Pi 5 (bcm2712) overlay** | Add bcm2712 `compatible` + Pi-5 I2S/clock variant; install.sh overlay logic; hardware test. | `setup/respeaker-2mic-v2_0-overlay.dts`, `install.sh` | M |
| **6 — AEC in container** *(stretch)* | Abstract AEC reference behind an interface; prefer host `module-echo-cancel`; verify `.monitor` over socket. | `core/aec_reference.py`, `core/aec_pipeline.py` | L |

After **Phase 1**: a more reliable 64-bit Pi (Goal A, HAT case). After **3+4**: a
working full-audio container on Linux/Pi hosts (Goal B, minus AEC). Pi 5 and AEC
are isolated tail risks that block nothing else.

## Open questions

1. **Target Pi OS — Bookworm (3.11) or Trixie (3.13)?** Recommend Trixie/3.13 to
   match the build image and skip on-device venv rebuilds.
2. **PulseAudio vs PipeWire-pulse on the container *host*?** A2DP-in needs real
   PulseAudio; everything else works under either. Acceptable to require/document
   PulseAudio on the host for full BT?
3. **Is the non-root container rework acceptable now** (it's the price of the
   shared-Pulse path and the cleanest transport)?

## Author lean

Do **0 → 1** first (low risk, fixes wake-word reliability, no container changes),
then **2 → 3 → 4** for the container, with **5 (Pi 5)** and **6 (AEC)** as
independent follow-ups. Verify AEC `.monitor`-over-socket empirically before
committing any AEC-in-container scope.
